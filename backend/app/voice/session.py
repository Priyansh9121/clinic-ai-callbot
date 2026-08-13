"""Voice orchestration — audio in, conversation, audio out.

This is the adapter that turns the existing *text* receptionist into a voice
one. It owns audio mechanics and nothing else:

    microphone → VAD → endpointing → STT → ConversationEngine → TTS → speaker

``ConversationEngine`` is called through its unchanged text contract
(``handle_message(session_id, text) -> TurnResult``). No appointment rule, no
verification rule, no safety rule and no prompt lives here — those already have
homes, and duplicating them in an audio layer is how the two copies drift.

Three mechanisms make it feel like a phone call rather than a walkie-talkie:

* **Full-duplex capture.** The microphone is never gated on playback, so caller
  speech is detected *while* the agent is talking. That is what barge-in needs.
* **Turn tokens.** Every stage carries the token of the turn it belongs to, and
  the token is re-checked immediately before any audio is queued. A turn that
  loses the race is discarded rather than spoken over its successor.
* **Acknowledgement while thinking.** Slow turns get a short pre-synthesised
  phrase so the caller hears something within a few hundred milliseconds
  instead of sitting in silence for the ~1.2 s the local LLM can take.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from app.conversation.engine import ConversationEngine, TurnResult
from app.core.logging import get_logger
from app.core.metrics import M, metrics
from app.speech.providers import (
    SpeechEvent,
    SpeechToTextProvider,
    TextToSpeechProvider,
    VoiceActivityDetector,
)
from app.voice.endpointing import EndpointConfig, Endpointer, EndpointReason
from app.voice.speech_text import for_speech
from app.voice.states import (
    INTERRUPTIBLE_STATES,
    StaleTurnError,
    TurnToken,
    TurnTracker,
    VoiceState,
)
from app.voice.transport import AudioInput, AudioOutput

logger = get_logger(__name__)

#: Spoken when STT returns nothing usable. Taken from the existing catalogue so
#: voice and text say the same thing.
NO_SPEECH_REPROMPT = "Sorry, I didn't catch that."

#: Said while a slow turn is processed. Must be in the TTS allowlist so it is
#: served from cache with no synthesis latency.
ACKNOWLEDGEMENT = "One moment while I check."

#: How long the caller may hear nothing, measured from the moment they stop
#: speaking. Endpointing and transcription are spent from this budget before the
#: engine is even called, which on this hardware is most of it — see
#: docs/architecture/voice-pipeline.md.
DEFAULT_ACKNOWLEDGEMENT_AFTER_MS = 900

#: Floor on the engine's share of that budget once the rest is spent. Sits well
#: above the deterministic path (~30 ms) so it keeps its silence, and well below
#: the local LLM (~1.3 s) so slow turns are still covered.
MIN_ACKNOWLEDGEMENT_WAIT_MS = 150

#: Consecutive no-speech turns before giving up, so a silent or dead line
#: cannot loop forever.
MAX_CONSECUTIVE_NO_SPEECH = 3


@dataclass(slots=True)
class VoiceTurnMetrics:
    """Latency breakdown for one turn. Timings only — never content."""

    turn_id: int
    endpoint_latency_ms: float = 0.0
    stt_ms: float = 0.0
    engine_ms: float = 0.0
    tts_first_audio_ms: float = 0.0
    #: Time from the end of the caller's speech to the first agent audio of
    #: *any* kind, acknowledgement included. This is perceived latency — the
    #: silence the caller actually sits in — and matters more than how long the
    #: final answer took.
    time_to_first_audio_ms: float = 0.0
    speech_ms: float = 0.0
    acknowledged: bool = False
    barge_in_latency_ms: float | None = None

    def as_log_fields(self) -> dict[str, object]:
        return {
            "turn": self.turn_id,
            "endpoint_ms": round(self.endpoint_latency_ms),
            "stt_ms": round(self.stt_ms),
            "engine_ms": round(self.engine_ms),
            "tts_first_ms": round(self.tts_first_audio_ms),
            "first_audio_ms": round(self.time_to_first_audio_ms),
            "acknowledged": self.acknowledged,
        }


@dataclass(slots=True)
class VoiceSessionConfig:
    endpoint: EndpointConfig = field(default_factory=EndpointConfig)
    acknowledgement_after_ms: int = DEFAULT_ACKNOWLEDGEMENT_AFTER_MS
    acknowledgement_enabled: bool = True
    #: Frames of caller speech required to treat speech-over-agent as a real
    #: barge-in. One frame is enough to react fast; more resists echo.
    barge_in_frames: int = 1
    max_consecutive_no_speech: int = MAX_CONSECUTIVE_NO_SPEECH
    greet_on_start: bool = True


class VoiceSession:
    """One caller. Owns its own audio state, turn numbering and cancellation.

    Nothing here is shared between sessions: two concurrent callers get
    separate VAD state, endpointers, turn trackers and buffers. Providers
    (STT/TTS models) *are* shared, and are internally concurrency-bounded.
    """

    def __init__(
        self,
        *,
        engine: ConversationEngine,
        audio_input: AudioInput,
        audio_output: AudioOutput,
        vad: VoiceActivityDetector,
        stt: SpeechToTextProvider,
        tts: TextToSpeechProvider,
        config: VoiceSessionConfig | None = None,
        session_id: str | None = None,
        on_event: Callable[[str, dict[str, object]], None] | None = None,
    ) -> None:
        self._engine = engine
        self._input = audio_input
        self._output = audio_output
        self._vad = vad
        self._stt = stt
        self._tts = tts
        self._config = config or VoiceSessionConfig()
        self._session_id = session_id or f"voice_{uuid.uuid4().hex[:12]}"

        self._state = VoiceState.IDLE
        self._turns = TurnTracker()
        self._endpointer = Endpointer(self._config.endpoint)
        self._utterance = bytearray()
        self._consecutive_no_speech = 0
        self._barge_in_frames = 0
        self._stop_requested = asyncio.Event()
        self._turn_metrics: list[VoiceTurnMetrics] = []
        # Notifies the CLI without this class knowing anything about a UI.
        self._on_event = on_event
        self._active_work: asyncio.Task[None] | None = None
        # *Every* scheduled task, not just the newest. A superseded turn keeps
        # running until it hits its next ensure_active check, and it may still
        # hold a database transaction — leaving it untracked meant shutdown
        # returned while work was in flight, which deadlocked schema teardown
        # in tests and would leak connections in production.
        self._tasks: set[asyncio.Task[None]] = set()

    # --- Introspection -----------------------------------------------------
    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def state(self) -> VoiceState:
        return self._state

    @property
    def turn_metrics(self) -> list[VoiceTurnMetrics]:
        return list(self._turn_metrics)

    def _set_state(self, state: VoiceState) -> None:
        if state is self._state:
            return
        self._state = state
        self._emit("state", {"state": state.value})

    def _emit(self, kind: str, payload: dict[str, object]) -> None:
        if self._on_event is not None:
            self._on_event(kind, payload)

    # --- Lifecycle ---------------------------------------------------------
    async def run(self) -> None:
        """Run the session until stopped. Returns after a clean teardown."""
        metrics.increment(M.VOICE_SESSIONS)
        await self._input.start()
        await self._output.start()
        self._set_state(VoiceState.LISTENING)

        try:
            if self._config.greet_on_start:
                await self._greet()
            await self._listen_loop()
            # The frame source ended (or stop was requested). Give a turn that
            # is still speaking a moment to finish, so the caller hears the
            # last reply rather than having it cut off by teardown.
            await self._await_active_work(limit_seconds=10.0)
        finally:
            await self._shutdown()

    async def stop(self) -> None:
        """Request shutdown. Safe from a signal handler or another task.

        Also stops the audio source, because the listen loop is usually parked
        waiting for the next frame — without this it would only notice the stop
        request after the caller happened to make a noise.
        """
        self._stop_requested.set()
        with contextlib.suppress(Exception):
            await self._input.stop()

    async def _shutdown(self) -> None:
        self._set_state(VoiceState.STOPPING)
        self._turns.cancel_current()
        await self._drain_tasks()
        await self._output.stop_immediately()
        await self._input.stop()
        await self._output.stop()
        logger.info("voice.session_ended", turns=len(self._turn_metrics))

    async def _greet(self) -> None:
        """Speak the greeting, which also creates the conversation session.

        Scheduled as background work rather than awaited, so the listen loop
        starts immediately and the caller can talk over the greeting.
        """
        result = await self._engine.start(channel="voice", session_id=self._session_id)
        token = self._turns.begin()
        await self._schedule(self._speak(result.reply, token))

    async def _schedule(self, coro: Awaitable[None]) -> asyncio.Task[None]:
        """Run turn work in the background so the listen loop keeps consuming.

        This is what makes barge-in possible at all: if the loop awaited each
        turn, no frames would be read while the agent was speaking and caller
        speech could not be detected until it had finished.

        The previous task is cancelled *and awaited* first. Turns must not
        overlap: they share one ``AsyncSession`` through the ConversationEngine,
        and SQLAlchemy forbids concurrent use of a session — two overlapping
        turns deadlocked on the connection rather than failing loudly.
        Cancellation is immediate, so this does not delay the loop.
        """
        await self._cancel_active()
        task = asyncio.create_task(_guard(coro))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        self._active_work = task
        return task

    async def _cancel_active(self) -> None:
        """Cancel the in-flight turn and wait for it to actually unwind."""
        work, self._active_work = self._active_work, None
        if work is None or work.done():
            return
        work.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await work

    async def _drain_tasks(self) -> None:
        """Cancel and await every scheduled task before releasing resources.

        Must await, not just cancel: a task cancelled mid-``await`` has not
        actually unwound yet, and any database work it holds stays open until
        it does.
        """
        pending, self._tasks = set(self._tasks), set()
        self._active_work = None
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    async def _await_active_work(self, *, limit_seconds: float) -> None:
        """Give in-flight turn work a bounded chance to finish."""
        work = self._active_work
        if work is None or work.done():
            return
        with contextlib.suppress(TimeoutError, asyncio.CancelledError):
            async with asyncio.timeout(limit_seconds):
                await asyncio.shield(work)

    # --- Main loop ---------------------------------------------------------
    async def _listen_loop(self) -> None:
        """Consume frames continuously: segment, endpoint, and dispatch turns.

        Never blocks on a turn. Capture continues during playback — that is
        what makes barge-in possible — so every frame is also tested against
        the interruption path.
        """
        async for frame in self._input.frames():
            if self._stop_requested.is_set():
                break

            event = self._vad.process_frame(frame)
            is_speech = bool(getattr(self._vad, "is_speaking", False))

            if self._state in INTERRUPTIBLE_STATES:
                if await self._maybe_barge_in(event, is_speech, frame):
                    continue
                # While the agent talks, frames are otherwise dropped: they are
                # the agent's own voice or room noise, not a caller turn.
                continue

            if is_speech or self._endpointer.speech_started:
                self._utterance.extend(frame)
                if self._state is VoiceState.LISTENING and is_speech:
                    self._set_state(VoiceState.SPEECH_DETECTED)

            endpoint = self._endpointer.process(event, is_speech=is_speech)
            if endpoint is None:
                continue

            metrics.observe(M.VAD_ENDPOINT_LATENCY, endpoint.endpoint_latency_ms)
            utterance = bytes(self._utterance)
            self._reset_utterance()

            if not endpoint.is_usable:
                await self._handle_unusable(endpoint.reason)
                continue

            await self._handle_turn(utterance, endpoint.endpoint_latency_ms, endpoint.speech_ms)

    def _reset_utterance(self) -> None:
        """Clear the utterance buffer, leaving the turn state alone.

        State is *not* forced to LISTENING here: a turn dispatched from this
        same frame owns the state from now on, and resetting it would stomp on
        TRANSCRIBING a moment later.
        """
        self._utterance.clear()
        self._endpointer.reset()
        self._vad.reset()

    # --- Barge-in ----------------------------------------------------------
    async def _maybe_barge_in(
        self, event: SpeechEvent | None, is_speech: bool, frame: bytes
    ) -> bool:
        """Stop the agent if the caller has started speaking over it.

        Returns True when an interruption was handled. Requires
        ``barge_in_frames`` consecutive speech frames so a single click or an
        echo of the agent's own voice does not cut it off.
        """
        if not is_speech and event is not SpeechEvent.SPEECH_STARTED:
            self._barge_in_frames = 0
            return False

        self._barge_in_frames += 1
        if self._barge_in_frames < self._config.barge_in_frames:
            return False

        started = time.perf_counter()
        self._set_state(VoiceState.INTERRUPTED)

        # 1. Silence the speaker first — everything else can wait, the caller
        #    is currently talking over the agent.
        await self._output.stop_immediately()
        stop_latency_ms = (time.perf_counter() - started) * 1000

        # 2. Cancel the turn. Any synthesis or engine work still running is now
        #    stale and will be discarded rather than played.
        self._turns.cancel_current()
        await self._cancel_active()

        metrics.increment(M.BARGE_IN)
        metrics.observe(M.VOICE_BARGE_IN_LATENCY, stop_latency_ms)
        logger.info("voice.barge_in", stop_ms=round(stop_latency_ms, 1))
        self._emit("barge_in", {"stop_ms": round(stop_latency_ms, 1)})

        # 3. Begin the caller's new utterance with the frame that triggered it,
        #    so the first syllable is not clipped from the transcript.
        self._barge_in_frames = 0
        self._endpointer.reset()
        self._utterance.clear()
        self._utterance.extend(frame)
        self._endpointer.process(SpeechEvent.SPEECH_STARTED, is_speech=True)
        self._set_state(VoiceState.SPEECH_DETECTED)
        return True

    # --- Turn handling -----------------------------------------------------
    async def _handle_unusable(self, reason: EndpointReason) -> None:
        """Nothing usable was said. Re-prompt, but not forever."""
        metrics.increment(M.VOICE_NO_SPEECH, labels={"reason": reason.value})
        self._consecutive_no_speech += 1
        if self._consecutive_no_speech > self._config.max_consecutive_no_speech:
            logger.info("voice.no_speech_limit", reason=reason.value)
            self._stop_requested.set()
            return
        self._set_state(VoiceState.LISTENING)
        if reason is EndpointReason.NO_SPEECH:
            # Genuine silence: say nothing and keep listening. Prompting into
            # an empty room is worse than waiting.
            return
        token = self._turns.begin()
        await self._schedule(self._speak(NO_SPEECH_REPROMPT, token))

    async def _handle_turn(
        self, utterance: bytes, endpoint_latency_ms: float, speech_ms: float
    ) -> None:
        """Dispatch one cancellable turn: transcribe, consult, speak.

        The turn itself runs in the background so the listen loop keeps reading
        frames and the caller can interrupt it. Only the hand-over is awaited:
        ``_schedule`` first unwinds the previous turn, which is what keeps two
        turns from touching the shared database session at once.
        """
        token = self._turns.begin()
        stats = VoiceTurnMetrics(
            turn_id=token.id,
            endpoint_latency_ms=endpoint_latency_ms,
            speech_ms=speech_ms,
        )
        await self._schedule(self._run_turn(utterance, token, stats))

    async def _run_turn(self, utterance: bytes, token: TurnToken, stats: VoiceTurnMetrics) -> None:
        # The caller's clock starts when they stop talking, not when the
        # endpointer notices — the ~500 ms of confirming silence is dead air to
        # them too, so it belongs inside the perceived-latency number.
        turn_started = time.perf_counter() - stats.endpoint_latency_ms / 1000
        try:
            self._set_state(VoiceState.TRANSCRIBING)
            transcript = await self._stt.transcribe(utterance)
            stats.stt_ms = transcript.latency_ms
            self._turns.ensure_active(token)

            text = _clean_transcript(transcript.text)
            if not text or self._is_hallucination(transcript.text, utterance):
                self._set_state(VoiceState.LISTENING)
                metrics.increment(M.STT_EMPTY)
                self._consecutive_no_speech += 1
                if self._consecutive_no_speech > self._config.max_consecutive_no_speech:
                    self._stop_requested.set()
                    return
                await self._speak(NO_SPEECH_REPROMPT, token)
                return

            self._consecutive_no_speech = 0
            # The transcript is logged nowhere; only its length is recorded.
            self._emit("transcript", {"chars": len(text), "text": text})

            self._set_state(VoiceState.THINKING)
            reply, stats.engine_ms, stats.acknowledged = await self._consult_engine(
                text, token, stats=stats, turn_started=turn_started
            )

            self._turns.ensure_active(token)
            await self._speak(reply.reply, token, stats=stats, turn_started=turn_started)

            metrics.increment(M.VOICE_TURNS)
            logger.info("voice.turn", **stats.as_log_fields())
            self._turn_metrics.append(stats)
            self._emit("turn", {"result": reply, "metrics": stats})

            if reply.ended:
                self._stop_requested.set()
        except StaleTurnError:
            metrics.increment(M.VOICE_STALE_RESPONSE_DISCARDED)
            logger.info("voice.stale_turn_discarded", turn=token.id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            metrics.increment(M.VOICE_ERROR, labels={"stage": "turn"})
            logger.error("voice.turn_failed", turn=token.id, error=type(exc).__name__)
            self._set_state(VoiceState.LISTENING)

    async def _consult_engine(
        self,
        text: str,
        token: TurnToken,
        *,
        stats: VoiceTurnMetrics | None = None,
        turn_started: float | None = None,
    ) -> tuple[TurnResult, float, bool]:
        """Call the ConversationEngine, covering a slow turn with speech.

        The acknowledgement is *not* played on every turn. It fires only if the
        caller has heard nothing by ``acknowledgement_after_ms``, so the
        deterministic path (tens of milliseconds) stays silent and only genuinely
        slow turns get filler.

        That deadline is measured from the caller's *speech end*, not from this
        call. Transcription has already spent ~440 ms of the caller's silence by
        the time we get here, and starting the clock now would bill them for it
        twice — measured at 1337 ms to first audio, against a 700 ms target.
        Anchoring to speech end brings the same acknowledgement forward without
        making it any more likely to fire on a fast turn.

        **The engine call is never cancelled.** Barge-in cancels the turn, but
        the engine touches the database, and aborting mid-statement leaves the
        AsyncSession unusable — the next turn then blocks on it forever, which
        is exactly what happened before this was shielded. So a superseded turn
        lets the engine finish and throws the *result* away instead. It is
        cheap to do: the engine is ~30 ms deterministically and ~1.2 s with the
        LLM, and no booking is committed without a spoken confirmation anyway.
        """
        started = time.perf_counter()
        engine_task = asyncio.create_task(self._engine.handle_message(self._session_id, text))
        acknowledged = False

        try:
            if self._config.acknowledgement_enabled:
                try:
                    result = await asyncio.wait_for(
                        asyncio.shield(engine_task),
                        timeout=self._acknowledgement_wait(turn_started),
                    )
                    return result, (time.perf_counter() - started) * 1000, acknowledged
                except TimeoutError:
                    if self._turns.is_active(token):
                        await self._speak(
                            ACKNOWLEDGEMENT,
                            token,
                            stats=stats,
                            turn_started=turn_started,
                            acknowledgement=True,
                        )
                        acknowledged = True
                        metrics.increment(M.VOICE_ACKNOWLEDGEMENT_PLAYED)

            result = await asyncio.shield(engine_task)
            return result, (time.perf_counter() - started) * 1000, acknowledged
        except asyncio.CancelledError:
            # Interrupted. Let the database work unwind cleanly before this
            # turn dies, then discard whatever it produced.
            with contextlib.suppress(Exception):
                await engine_task
            raise

    def _acknowledgement_wait(self, turn_started: float | None) -> float:
        """Seconds to give the engine before filling the silence.

        The budget belongs to the caller's silence as a whole, so whatever
        endpointing and transcription already spent comes out of it.
        """
        budget = self._config.acknowledgement_after_ms / 1000
        if turn_started is None:
            return budget
        remaining = budget - (time.perf_counter() - turn_started)
        # Never hand the engine a zero timeout. A deterministic reply lands in
        # ~10 ms and should still win the race after a slow transcription —
        # otherwise every long utterance picks up filler it does not need.
        return max(MIN_ACKNOWLEDGEMENT_WAIT_MS / 1000, remaining)

    def _is_hallucination(self, text: str, utterance: bytes) -> bool:
        from app.speech.stt.whisper import looks_like_silence_hallucination

        if looks_like_silence_hallucination(text, utterance):
            metrics.increment(M.STT_HALLUCINATION_DISCARDED)
            logger.info("voice.stt_hallucination_discarded")
            return True
        return False

    # --- Speaking ----------------------------------------------------------
    async def _speak(
        self,
        text: str,
        token: TurnToken,
        *,
        stats: VoiceTurnMetrics | None = None,
        turn_started: float | None = None,
        acknowledgement: bool = False,
    ) -> None:
        """Synthesise and play an approved reply, abandoning it if superseded.

        Only text the ConversationEngine produced reaches here. Raw model
        output, exceptions and provider errors never do — the engine already
        converts every failure into an approved caller-facing message.
        """
        spoken = for_speech(text)
        if not spoken:
            return

        self._turns.ensure_active(token)
        self._set_state(VoiceState.SPEAKING)
        started = time.perf_counter()
        first_chunk = True

        try:
            async for chunk in self._tts.synthesize_stream(spoken):
                # Re-checked per chunk: a barge-in between two chunks must stop
                # the rest of the sentence reaching the speaker.
                self._turns.ensure_active(token)
                if first_chunk:
                    first_chunk = False
                    first_audio_ms = (time.perf_counter() - started) * 1000
                    if stats is not None:
                        if not acknowledgement:
                            stats.tts_first_audio_ms = first_audio_ms
                        # Recorded once per turn, on whichever audio reaches the
                        # caller first — usually the acknowledgement on a slow
                        # turn. Overwriting it with the final reply's timing
                        # would report silence the caller never experienced.
                        if turn_started is not None and stats.time_to_first_audio_ms == 0.0:
                            stats.time_to_first_audio_ms = (
                                time.perf_counter() - turn_started
                            ) * 1000
                            metrics.observe(
                                M.VOICE_TIME_TO_FIRST_AUDIO, stats.time_to_first_audio_ms
                            )
                await self._output.play(chunk)
            await self._output.wait_until_drained()
        except StaleTurnError:
            # Superseded mid-sentence. Playback was already stopped by the
            # barge-in handler; nothing further should be queued.
            metrics.increment(M.VOICE_STALE_RESPONSE_DISCARDED)
            raise
        finally:
            if not acknowledgement and self._turns.is_active(token):
                self._set_state(VoiceState.LISTENING)

    # --- Utilities ---------------------------------------------------------
    async def speak_now(self, text: str) -> None:
        """Speak outside the turn machinery. CLI and tests only."""
        token = self._turns.begin()
        await self._speak(text, token)


def _clean_transcript(text: str) -> str:
    """Bound and sanitise STT output before it becomes a caller turn.

    STT output is untrusted input in exactly the same way typed input is. It is
    bounded and stripped of control characters here; the schema and the
    conversation engine apply the same limits they always have, so a spoken
    prompt injection has precisely the structural reach of a typed one — none.
    """
    from app.schemas.conversation import MAX_MESSAGE_LENGTH

    if not text:
        return ""
    cleaned = "".join(ch for ch in text if ch.isprintable() or ch in " \t")
    return cleaned.strip()[:MAX_MESSAGE_LENGTH]


async def _guard(coro: Awaitable[None]) -> None:
    """Run background turn work, absorbing the expected failure modes.

    A cancelled turn (barge-in) and a superseded turn (stale) are both normal
    outcomes here, not errors — they must not surface as unhandled task
    exceptions that would spam the log or take the session down.
    """
    try:
        await coro
    except asyncio.CancelledError:
        metrics.increment(M.VOICE_TURN_CANCELLED)
    except StaleTurnError:
        metrics.increment(M.VOICE_STALE_RESPONSE_DISCARDED)
    except Exception as exc:
        metrics.increment(M.VOICE_ERROR, labels={"stage": "background"})
        logger.error("voice.background_task_failed", error=type(exc).__name__)
