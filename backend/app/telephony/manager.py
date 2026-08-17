"""One call, one isolated session — and the wiring that keeps it that way.

A ``CallSession`` owns everything belonging to a single caller: its own
database session, ``ConversationEngine``, ``VoiceSession``, media channel, VAD
state, endpointer, turn tracker and cancellation state. Nothing is shared
between concurrent calls except the speech models and the LLM provider, both of
which are stateless per call and internally concurrency-bounded.

The conversation layer is not modified and is not aware of telephony. The link
is the ``on_event`` callback ``VoiceSession`` already emits: a completed turn
carries ``requires_human`` and ``ended``, which this module translates into a
transfer or a hangup. Control flows one way — telephony reacts to conversation
decisions, never the reverse.

Ordering matters and is worth stating, because it is what stops a caller being
cut off mid-sentence: ``VoiceSession._speak`` awaits ``wait_until_drained``
before the turn event fires, and for telephony "drained" means the pacer has
actually transmitted the audio. So by the time this module sees ``ended=True``,
the goodbye has left the process.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum

from app.conversation.responses import TRANSFER_UNAVAILABLE
from app.core.logging import get_logger
from app.core.metrics import M, metrics
from app.telephony.control import CallController, HangupCause, TransferResult
from app.telephony.destinations import DestinationRegistry, UnknownDestination
from app.telephony.metadata import CallerIdentity, CallMetadata
from app.telephony.states import CallState, IllegalCallTransition, next_state
from app.telephony.transport import AudioSocketChannel, AudioSocketInput, AudioSocketOutput
from app.voice.session import VoiceSession

logger = get_logger(__name__)

#: Time allowed for the last audio to clear Asterisk's own buffers before the
#: channel is released. Without it "Thank you, goodbye." can be clipped by the
#: hangup; with too much of it, callers sit on a dead line.
DEFAULT_HANGUP_DRAIN_MS = 250

#: Hard ceiling on one call. A receptionist call that has not finished in ten
#: minutes is not going to: the caller has walked away, the line is holding
#: open on silence, or a turn is wedged. Every one of those holds a media slot,
#: a database session and a slice of the concurrency cap until the process
#: restarts, so the call is ended rather than left to run forever.
DEFAULT_MAX_CALL_SECONDS = 600

#: A caller who hangs up must not leave inference running. This bounds how long
#: teardown waits for cancelled work to unwind before giving up on it.
TEARDOWN_TIMEOUT_SECONDS = 5.0


class CapacityPolicy(StrEnum):
    """What to do when the machine is already at its call limit.

    Deliberately configuration, not a default the code invents: "busy" and
    "send them to a human" are clinic decisions with different consequences.
    """

    #: Do not answer. The dialplan's existing health-check failover routes the
    #: caller to reception — the arrangement documented since Milestone 1.
    REJECT = "reject"
    #: Answer and transfer straight to reception.
    TRANSFER_RECEPTION = "transfer_reception"


@dataclass(slots=True)
class CallStats:
    """Timings for one call. No content, ever."""

    call_id: str
    started_at: float = field(default_factory=time.perf_counter)
    answered_at: float | None = None
    first_audio_at: float | None = None
    ended_at: float | None = None
    turns: int = 0
    transferred: bool = False
    dtmf_digits: int = 0

    @property
    def answer_to_first_audio_ms(self) -> float | None:
        if self.answered_at is None or self.first_audio_at is None:
            return None
        return (self.first_audio_at - self.answered_at) * 1000

    @property
    def duration_seconds(self) -> float:
        end = self.ended_at if self.ended_at is not None else time.perf_counter()
        return end - self.started_at


class CallSession:
    """A single telephone call, from answer to cleanup."""

    def __init__(
        self,
        *,
        call_id: str,
        controller: CallController,
        channel: AudioSocketChannel,
        voice_session_factory: Callable[
            [AudioSocketInput, AudioSocketOutput, Callable], VoiceSession
        ],
        destinations: DestinationRegistry,
        hangup_drain_ms: int = DEFAULT_HANGUP_DRAIN_MS,
        max_call_seconds: int = DEFAULT_MAX_CALL_SECONDS,
    ) -> None:
        self._call_id = call_id
        self._controller = controller
        self._channel = channel
        self._destinations = destinations
        self._hangup_drain_ms = hangup_drain_ms
        self._max_call_seconds = max_call_seconds

        self._state = CallState.RINGING
        self._stats = CallStats(call_id=call_id)
        self._voice: VoiceSession | None = None
        self._voice_factory = voice_session_factory
        self._transfer_target = None
        self._dtmf: list[str] = []
        self._ended = asyncio.Event()
        self._finished = False

    # --- Introspection -----------------------------------------------------
    @property
    def call_id(self) -> str:
        return self._call_id

    @property
    def state(self) -> CallState:
        return self._state

    @property
    def stats(self) -> CallStats:
        return self._stats

    @property
    def voice(self) -> VoiceSession | None:
        """The voice session this call is running, once it has been built."""
        return self._voice

    @property
    def dtmf_digits(self) -> str:
        """Digits pressed so far.

        Structured input, kept separate from speech and never sent through STT
        or into the conversation as text. Milestone 5 (OTP) is the consumer;
        Milestone 4 only proves the transport can deliver it.
        """
        return "".join(self._dtmf)

    def _move(self, state: CallState) -> None:
        try:
            self._state = next_state(self._state, state)
        except IllegalCallTransition as exc:
            # Log rather than raise: a late PBX event racing our own teardown
            # is normal, and must not turn into an exception during cleanup.
            logger.debug("telephony.illegal_transition", call_id=self._call_id, error=str(exc))
            return
        logger.debug("telephony.call_state", call_id=self._call_id, state=state.value)

    # --- Lifecycle ---------------------------------------------------------
    async def run(self) -> None:
        """Answer, converse, then tear everything down. Always cleans up."""
        metrics.increment(M.TELEPHONY_CALLS_STARTED)
        audio_in = AudioSocketInput(self._channel)
        audio_out = AudioSocketOutput(self._channel)

        self._controller.on_hangup(self._on_caller_hangup)
        self._controller.on_dtmf(lambda event: self._record_dtmf(event.digit))
        self._channel.on_dtmf(self._record_dtmf)
        self._channel.on_hangup(lambda: self._on_caller_hangup(HangupCause.CALLER))

        try:
            self._move(CallState.ANSWERING)
            await self._controller.answer()
            self._stats.answered_at = time.perf_counter()
            self._move(CallState.CONNECTED)

            self._voice = self._voice_factory(audio_in, audio_out, self._on_voice_event)
            self._move(CallState.ACTIVE)
            try:
                async with asyncio.timeout(self._max_call_seconds):
                    await self._voice.run()
            except TimeoutError:
                # Cancels the conversation where it stands and falls through to
                # finish(), which is the same teardown a caller hangup takes —
                # channel released, media closed, capacity returned, database
                # session handed back. Nothing here is special-cased, so the
                # cleanup cannot drift out of step with the ordinary path.
                metrics.increment(M.TELEPHONY_CALLS_FAILED, labels={"stage": "duration_limit"})
                logger.warning(
                    "telephony.call_duration_limit",
                    call_id=self._call_id,
                    limit_seconds=self._max_call_seconds,
                )
                self._move(CallState.FAILED)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            metrics.increment(M.TELEPHONY_CALLS_FAILED, labels={"stage": "run"})
            logger.error("telephony.call_failed", call_id=self._call_id, error=type(exc).__name__)
            self._move(CallState.FAILED)
        finally:
            await self.finish()

    def _on_voice_event(self, kind: str, payload: dict) -> None:
        """Bridge from the voice layer. Must not block — it runs inline."""
        if kind == "turn":
            self._stats.turns += 1
            self._log_turn_latency(payload.get("metrics"))
            result = payload.get("result")
            if result is not None and getattr(result, "requires_human", False):
                # Scheduled, not awaited: this callback is invoked from inside
                # the turn, and transferring here would deadlock on it.
                self._transfer_target = getattr(result, "transfer_reason", None)
                asyncio.get_running_loop().create_task(self._transfer_now())
        elif kind == "state" and self._stats.first_audio_at is None:
            if payload.get("state") == "SPEAKING":
                self._stats.first_audio_at = time.perf_counter()
                if (elapsed := self._stats.answer_to_first_audio_ms) is not None:
                    metrics.observe(M.TELEPHONY_ANSWER_TO_FIRST_AUDIO, elapsed)

    def _log_turn_latency(self, stats: object) -> None:
        """Emit one line carrying the whole speech-end -> on-the-wire breakdown.

        The two halves are measured in different layers on purpose. VoiceSession
        knows endpointing, STT, engine and synthesis but cannot see a socket;
        the transport knows what the resampler, the queue and the pacer cost but
        has no idea what a turn is. Neither seam is widened to join them —
        CallSession is the one object that legitimately holds both, so the join
        happens here.

        `to_wire_ms` is the honest end of what this process can measure. What
        the caller actually waits for additionally includes Asterisk's own
        buffering, the SIP leg and the handset's jitter buffer, none of which
        are observable from here — that difference needs a packet capture or a
        stopwatch and a human ear.
        """
        if stats is None:
            return
        to_first_audio = getattr(stats, "time_to_first_audio_ms", 0.0)
        wire_ms = self._channel.last_reply_wire_latency_ms
        logger.info(
            "telephony.turn_latency",
            call_id=self._call_id,
            turn=getattr(stats, "turn_id", None),
            endpoint_ms=round(getattr(stats, "endpoint_latency_ms", 0.0)),
            stt_ms=round(getattr(stats, "stt_ms", 0.0)),
            engine_ms=round(getattr(stats, "engine_ms", 0.0)),
            tts_first_audio_ms=round(getattr(stats, "tts_first_audio_ms", 0.0)),
            acknowledged=getattr(stats, "acknowledged", False),
            to_first_audio_ms=round(to_first_audio),
            wire_ms=round(wire_ms) if wire_ms is not None else None,
            to_wire_ms=round(to_first_audio + wire_ms) if wire_ms is not None else None,
        )

    def _record_dtmf(self, digit: str) -> None:
        # Bounded: a caller leaning on the keypad must not grow a list forever.
        if len(self._dtmf) < 32:
            self._dtmf.append(digit)
        self._stats.dtmf_digits += 1

    async def _transfer_now(self) -> TransferResult | None:
        """Hand the caller to the approved destination for this reason."""
        if self._state in (CallState.TRANSFERRING, CallState.HANGING_UP):
            return None
        self._move(CallState.TRANSFERRING)
        try:
            target = self._destinations.for_reason(self._transfer_target)
        except UnknownDestination as exc:
            logger.error("telephony.no_destination", call_id=self._call_id, error=str(exc))
            metrics.increment(M.TELEPHONY_TRANSFER_FAILED, labels={"destination": "unresolved"})
            self._move(CallState.ACTIVE)
            return None

        result = await self._controller.transfer(target)
        if result.succeeded:
            self._stats.transferred = True
            # Mark the call finished *before* awaiting anything. A successful
            # transfer takes the channel out of Stasis, so Asterisk emits
            # StasisEnd immediately — and if that lands while we are still
            # stopping the voice session, it would be recorded as a caller
            # hangup. The caller did not hang up; they were handed to a human.
            self._ended.set()
            # The channel now belongs to the dialplan. Stop our voice session
            # so we are not still listening to a caller talking to a human.
            if self._voice is not None:
                await self._voice.stop()
            return result

        # Transfer failed. The caller is still with us and must be told
        # something, in approved wording, rather than dropped.
        metrics.increment(M.TELEPHONY_TRANSFER_FAILED, labels={"destination": "unavailable"})
        self._move(CallState.ACTIVE)
        if self._voice is not None:
            with contextlib.suppress(Exception):
                await self._voice.speak_now(TRANSFER_UNAVAILABLE)
        return result

    def _on_caller_hangup(self, cause: HangupCause = HangupCause.CALLER) -> None:
        """The caller is gone. Stop everything; do not finish the sentence."""
        if self._ended.is_set():
            return
        self._ended.set()
        metrics.increment(M.TELEPHONY_DISCONNECTS, labels={"cause": cause.value})
        logger.info("telephony.caller_hangup", call_id=self._call_id)
        if self._voice is not None:
            # Scheduled from a PBX/socket callback, so it must not block here.
            asyncio.get_running_loop().create_task(_suppress(self._voice.stop()))

    async def finish(self) -> None:
        """Release every resource this call held. Runs exactly once.

        Guarded on having *run*, not on the call being in a terminal state.
        Those are different questions, and conflating them meant a call that
        reached FAILED never released anything: run()'s error path moves to
        FAILED and then calls finish() from its finally block, which saw a
        terminal state and returned immediately. The channel was never hung up,
        the media socket never closed, and the capacity slot never returned —
        on every failed call, which are exactly the calls that most need
        cleaning up.
        """
        if self._finished:
            return
        self._finished = True

        # A call that failed stays FAILED: it did not complete, and the metrics
        # must not say it did. Only the drain is skipped — there is no last
        # audio worth waiting for when the conversation is already gone.
        failed = self._state is CallState.FAILED
        if not failed and self._state is not CallState.TRANSFERRING:
            self._move(CallState.HANGING_UP)
            # Let the last audio clear Asterisk before the channel goes.
            if self._hangup_drain_ms > 0 and self._channel.is_running:
                await asyncio.sleep(self._hangup_drain_ms / 1000)

        with contextlib.suppress(Exception):
            await asyncio.wait_for(
                self._controller.hangup(
                    HangupCause.TRANSFERRED if self._stats.transferred else HangupCause.APPLICATION
                ),
                timeout=TEARDOWN_TIMEOUT_SECONDS,
            )
        with contextlib.suppress(Exception):
            await asyncio.wait_for(self._channel.close(), timeout=TEARDOWN_TIMEOUT_SECONDS)

        self._stats.ended_at = time.perf_counter()
        if not failed:
            self._move(CallState.ENDED)
            metrics.increment(M.TELEPHONY_CALLS_COMPLETED)
        self._ended.set()
        metrics.observe(M.TELEPHONY_CALL_DURATION, self._stats.duration_seconds * 1000)
        logger.info(
            "telephony.call_ended",
            call_id=self._call_id,
            turns=self._stats.turns,
            duration_s=round(self._stats.duration_seconds, 1),
            transferred=self._stats.transferred,
        )


async def _suppress(coro: Awaitable[None]) -> None:
    with contextlib.suppress(Exception, asyncio.CancelledError):
        await coro


class CallManager:
    """Tracks live calls and enforces capacity.

    The registry is per-process and keyed by ``call_id``. Nothing is keyed by
    phone number: two callers from the same office switchboard are two calls.
    """

    def __init__(
        self,
        *,
        max_active_calls: int = 2,
        capacity_policy: CapacityPolicy = CapacityPolicy.REJECT,
    ) -> None:
        self._calls: dict[str, CallSession] = {}
        self._max_active = max_active_calls
        self._policy = capacity_policy
        self._lock = asyncio.Lock()

    @property
    def active_calls(self) -> int:
        return len(self._calls)

    @property
    def max_active_calls(self) -> int:
        return self._max_active

    @property
    def capacity_policy(self) -> CapacityPolicy:
        return self._policy

    @property
    def at_capacity(self) -> bool:
        return len(self._calls) >= self._max_active

    def get(self, call_id: str) -> CallSession | None:
        return self._calls.get(call_id)

    def call_ids(self) -> tuple[str, ...]:
        return tuple(self._calls)

    async def register(self, session: CallSession) -> bool:
        """Admit a call if there is capacity. Returns False when full."""
        async with self._lock:
            if len(self._calls) >= self._max_active:
                metrics.increment(M.TELEPHONY_CALLS_REJECTED, labels={"reason": "capacity"})
                logger.warning(
                    "telephony.at_capacity",
                    active=len(self._calls),
                    limit=self._max_active,
                    policy=self._policy.value,
                )
                return False
            self._calls[session.call_id] = session
            metrics.gauge(M.TELEPHONY_ACTIVE_CALLS, len(self._calls))
            return True

    async def release(self, call_id: str) -> None:
        async with self._lock:
            self._calls.pop(call_id, None)
            metrics.gauge(M.TELEPHONY_ACTIVE_CALLS, len(self._calls))

    async def shutdown(self) -> None:
        """End every live call. Used when the process is stopping."""
        for call_id in list(self._calls):
            session = self._calls.get(call_id)
            if session is not None:
                with contextlib.suppress(Exception):
                    await session.finish()
        self._calls.clear()
        metrics.gauge(M.TELEPHONY_ACTIVE_CALLS, 0)


def new_call_id() -> str:
    """Opaque correlation id. Never derived from the caller's number."""
    return uuid.uuid4().hex[:16]


def build_caller(number: str | None, name: str | None = None) -> CallerIdentity:
    return CallerIdentity.from_channel(number=number, name=name)


def build_metadata(call_id: str, *, extension: str | None, channel: str | None) -> CallMetadata:
    return CallMetadata(call_id=call_id, dialed_extension=extension, channel=channel)
