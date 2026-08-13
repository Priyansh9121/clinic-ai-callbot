#!/usr/bin/env python
"""End-to-end voice pipeline benchmark, with real local engines.

    uv run python scripts/benchmark_voice.py
    uv run python scripts/benchmark_voice.py --sweep-endpointing
    uv run python scripts/benchmark_voice.py --turns 5

Runs the *real* VAD, Whisper and Piper through the real ``VoiceSession``, using
a synthetic audio transport instead of a microphone. That keeps the measurement
reproducible and privacy-safe — the "caller" is Piper reading scripted clinic
phrases — while still exercising every component the live demo uses.

Latency is reported per component, not as one number, because the components
have very different fixes:

    speech end → endpoint      VAD hangover + endpoint silence
    endpoint   → transcript    Whisper
    transcript → engine reply  deterministic path, or the local LLM
    reply      → first audio   Piper
    speech end → first audio   what the caller actually experiences

Every number printed is measured on this machine. Nothing is estimated.
"""

from __future__ import annotations

import argparse
import asyncio
import statistics
import sys
import time
from collections.abc import AsyncIterator, Callable
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend"))

from app.clinic.knowledge_data import build_dev_knowledge  # noqa: E402
from app.conversation.engine import ConversationEngine  # noqa: E402
from app.conversation.store import InMemorySessionStore  # noqa: E402
from app.core.config import get_settings  # noqa: E402
from app.core.logging import configure_logging  # noqa: E402
from app.database.session import dispose_engine, session_scope  # noqa: E402
from app.llm.factory import build_llm_provider, build_message_analyser  # noqa: E402
from app.safety.engine import SafetyEngine  # noqa: E402
from app.speech import audio as audio_utils  # noqa: E402
from app.speech.factory import build_stt, build_tts, build_vad  # noqa: E402
from app.speech.providers import SpeechEvent  # noqa: E402
from app.voice.endpointing import EndpointConfig, Endpointer  # noqa: E402
from app.voice.factory import session_config  # noqa: E402
from app.voice.session import VoiceSession  # noqa: E402

BOLD, DIM, GREEN, YELLOW, RESET = "\033[1m", "\033[2m", "\033[32m", "\033[33m", "\033[0m"

#: Scripted caller turns. Synthetic clinic phrases — no real caller audio.
TURNS: list[str] = [
    "I was hoping to see Doctor Patel sometime Friday afternoon",
    "I've been there before",
    "Hughes",
    "fourteen March nineteen fifty eight",
    "a standard appointment",
]


class ScriptedAudioInput:
    """Feeds pre-rendered speech through the real ``AudioInput`` contract.

    Models a *polite* caller: after speaking, it keeps the line open with
    silence until the agent has finished replying, then speaks again. Talking
    over the agent would be barge-in — correct behaviour, but it cancels the
    turn, so a fixed gap measures interruption handling rather than turn
    latency. Barge-in is measured separately in :func:`measure_barge_in`.
    """

    def __init__(
        self,
        utterances: list[bytes],
        *,
        gap_ms: int = 400,
        turn_complete: Callable[[int], bool] | None = None,
        max_wait_s: float = 15.0,
    ) -> None:
        self._utterances = utterances
        self._gap_ms = gap_ms
        self._turn_complete = turn_complete
        self._max_wait_s = max_wait_s
        self._running = False
        #: Wall-clock instant each utterance's audio finished being fed.
        self.speech_end_times: list[float] = []
        #: Utterances whose turn never completed inside ``max_wait_s``.
        self.timed_out = 0

    @property
    def is_running(self) -> bool:
        return self._running

    async def start(self) -> None:
        self._running = True

    async def _silence(self, ms: int) -> AsyncIterator[bytes]:
        for frame in audio_utils.iter_frames(audio_utils.silence(ms)):
            if not self._running:
                return
            yield frame
            await asyncio.sleep(0.02)

    async def frames(self) -> AsyncIterator[bytes]:
        # Lead-in silence so the VAD settles before the first word.
        async for frame in self._silence(300):
            yield frame
        for index, utterance in enumerate(self._utterances):
            for frame in audio_utils.iter_frames(utterance):
                if not self._running:
                    return
                yield frame
                # Real time, so endpoint latency means something.
                await asyncio.sleep(0.02)
            self.speech_end_times.append(time.perf_counter())

            async for frame in self._silence(self._gap_ms):
                yield frame
            if self._turn_complete is None:
                continue
            # Hold the line open — in 200 ms of silence at a time — until this
            # turn's reply has been spoken. Stays well inside the endpointer's
            # initial-silence window, so waiting never fakes a no-speech turn.
            waited = 0.0
            while self._running and not self._turn_complete(index) and waited < self._max_wait_s:
                async for frame in self._silence(200):
                    yield frame
                waited += 0.2
            if not self._turn_complete(index):
                self.timed_out += 1

    async def stop(self) -> None:
        self._running = False


class NullAudioOutput:
    """Discards audio but records when the first chunk of each reply arrived."""

    def __init__(self) -> None:
        self._running = False
        self._playing = False
        self.first_audio_times: list[float] = []
        self._expecting_first = True

    @property
    def is_playing(self) -> bool:
        return self._playing

    async def start(self) -> None:
        self._running = True

    async def play(self, pcm: bytes) -> None:
        if self._expecting_first:
            self.first_audio_times.append(time.perf_counter())
            self._expecting_first = False
        self._playing = True

    async def wait_until_drained(self) -> None:
        self._playing = False
        self._expecting_first = True

    async def stop_immediately(self) -> None:
        self._playing = False
        self._expecting_first = True

    async def stop(self) -> None:
        self._running = False


def percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(int(round(pct / 100 * (len(ordered) - 1))), len(ordered) - 1)
    return ordered[index]


def summarise(label: str, values: list[float], *, target: str = "") -> None:
    if not values:
        print(f"  {label:<28} —")
        return
    print(
        f"  {label:<28} median {statistics.median(values):7.0f} ms | "
        f"p95 {percentile(values, 95):7.0f} ms | max {max(values):7.0f} ms"
        + (f"   {DIM}target {target}{RESET}" if target else "")
    )


async def render_turns(tts, texts: list[str]) -> list[bytes]:  # noqa: ANN001
    """Render the scripted caller turns with the local TTS engine."""
    rendered = []
    for text in texts:
        rendered.append(await tts.synthesize(text))
    return rendered


async def measure_vad(vad, utterances: list[bytes]) -> tuple[list[float], list[float]]:  # noqa: ANN001
    """Speech-start detection lag and endpoint lag, in isolation."""
    starts: list[float] = []
    ends: list[float] = []
    lead_ms = 300
    for utterance in utterances:
        vad.reset()
        endpointer = Endpointer(EndpointConfig(end_silence_ms=500, min_speech_ms=100))
        stream = audio_utils.silence(lead_ms) + utterance + audio_utils.silence(1200)
        speech_end_ms = lead_ms + audio_utils.duration_ms(utterance)
        detected_at: float | None = None
        for index, frame in enumerate(audio_utils.iter_frames(stream)):
            elapsed_ms = (index + 1) * 20
            event = vad.process_frame(frame)
            if event is SpeechEvent.SPEECH_STARTED and detected_at is None:
                detected_at = elapsed_ms
                starts.append(elapsed_ms - lead_ms)
            endpoint = endpointer.process(event, is_speech=bool(vad.is_speaking))
            if endpoint is not None:
                ends.append(elapsed_ms - speech_end_ms)
                break
    return starts, ends


async def measure_barge_in(vad, utterances: list[bytes]) -> list[float]:  # noqa: ANN001
    """Time from caller speech starting to playback actually stopping.

    Measured through the real ``SpeakerAudioOutput`` if a device is available,
    since the number that matters is how long the *device* keeps making noise.
    """
    from app.voice.devices import SpeakerAudioOutput

    latencies: list[float] = []
    speaker = SpeakerAudioOutput()
    try:
        await speaker.start()
    except Exception as exc:
        print(f"  {YELLOW}barge-in: no output device ({type(exc).__name__}){RESET}")
        return latencies

    try:
        for utterance in utterances[:3]:
            await speaker.play(utterance)
            await asyncio.sleep(0.05)
            started = time.perf_counter()
            await speaker.stop_immediately()
            latencies.append((time.perf_counter() - started) * 1000)
    finally:
        await speaker.stop()
    return latencies


async def sweep_endpointing(vad, utterances: list[bytes]) -> None:  # noqa: ANN001
    """Measure the cost of each end-of-turn silence threshold.

    The trade-off is real in both directions: too short and a caller pausing
    mid-sentence gets cut off, too long and the agent feels slow. This reports
    the latency cost and whether any utterance was split.
    """
    print(f"\n{BOLD}Endpointing sweep{RESET}")
    print(f"  {DIM}cuts = utterances split into more than one turn{RESET}")
    for threshold in (300, 500, 700):
        lags: list[float] = []
        cuts = 0
        for utterance in utterances:
            vad.reset()
            endpointer = Endpointer(
                EndpointConfig(end_silence_ms=threshold, min_speech_ms=100)
            )
            stream = audio_utils.silence(300) + utterance + audio_utils.silence(1500)
            speech_end_ms = 300 + audio_utils.duration_ms(utterance)
            endpoints = 0
            for index, frame in enumerate(audio_utils.iter_frames(stream)):
                elapsed_ms = (index + 1) * 20
                event = vad.process_frame(frame)
                endpoint = endpointer.process(event, is_speech=bool(vad.is_speaking))
                if endpoint is not None:
                    endpoints += 1
                    if endpoints == 1:
                        lags.append(elapsed_ms - speech_end_ms)
                        if elapsed_ms < speech_end_ms - 100:
                            cuts += 1
                    endpointer.reset()
            if endpoints > 1:
                cuts += endpoints - 1
        marker = GREEN if cuts == 0 else YELLOW
        print(
            f"  {threshold} ms end-silence : median lag "
            f"{statistics.median(lags) if lags else 0:6.0f} ms   "
            f"{marker}cuts {cuts}{RESET}"
        )


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--turns", type=int, default=len(TURNS))
    parser.add_argument("--sweep-endpointing", action="store_true")
    args = parser.parse_args()

    settings = get_settings()
    configure_logging(settings)

    stt, tts, vad = build_stt(settings), build_tts(settings), build_vad(settings)
    if not (stt and tts and vad):
        print("voice providers are not configured; set STT/TTS/VAD in .env")
        return 2

    print(f"{BOLD}Voice pipeline benchmark{RESET}")
    print(f"{DIM}stt={settings.stt_model} tts={Path(settings.tts_voice).stem} "
          f"vad={settings.vad_provider} llm={settings.effective_llm_mode}")
    print(f"synthetic caller audio (Piper) — no recorded speech{RESET}\n")

    texts = TURNS[: args.turns]
    print(f"{DIM}rendering {len(texts)} caller utterances…{RESET}")
    utterances = await render_turns(tts, texts)
    total_audio_s = sum(audio_utils.duration_seconds(u) for u in utterances)

    print(f"\n{BOLD}VAD{RESET}")
    starts, ends = await measure_vad(vad, utterances)
    summarise("speech-start detection", starts, target="< 150 ms")
    summarise("endpoint after speech", ends)

    if args.sweep_endpointing:
        await sweep_endpointing(vad, utterances)

    print(f"\n{BOLD}STT ({settings.stt_model}){RESET}")
    stt_latencies = []
    for utterance in utterances:
        started = time.perf_counter()
        await stt.transcribe(utterance)
        stt_latencies.append((time.perf_counter() - started) * 1000)
    summarise("transcribe (warm)", stt_latencies, target="< 500 ms")
    print(f"  {'real-time factor':<28} "
          f"{sum(stt_latencies) / 1000 / total_audio_s:.3f}  {DIM}(lower is faster){RESET}")

    print(f"\n{BOLD}TTS ({Path(settings.tts_voice).stem}){RESET}")
    first_audio, total = [], []
    for text in ["One moment while I check.", "Are you a new or existing patient?",
                 "I have Friday at 8 am, 9 am or 11:45 am. Which suits?"]:
        started = time.perf_counter()
        first = None
        async for _ in tts.synthesize_stream(text):
            if first is None:
                first = (time.perf_counter() - started) * 1000
        first_audio.append(first or 0.0)
        total.append((time.perf_counter() - started) * 1000)
    summarise("time to first audio", first_audio, target="< 300 ms")
    summarise("total synthesis", total)

    print(f"\n{BOLD}Barge-in (real output device){RESET}")
    barge = await measure_barge_in(vad, utterances)
    summarise("playback stop", barge, target="< 200 ms")

    # --- Full pipeline through the real VoiceSession ------------------------
    print(f"\n{BOLD}End-to-end turns{RESET}")
    store = InMemorySessionStore(ttl_seconds=600)
    llm_provider = build_llm_provider(settings)
    analyser = build_message_analyser(settings, provider=llm_provider)

    # The session emits "turn" only after the reply has finished playing, so
    # this counter is exactly "the agent has stopped talking".
    completed = 0

    def on_event(kind: str, payload: dict) -> None:
        nonlocal completed
        if kind == "turn":
            completed += 1

    audio_out = NullAudioOutput()
    audio_in = ScriptedAudioInput(utterances, turn_complete=lambda index: completed > index)
    async with session_scope() as db:
        engine = ConversationEngine(
            db=db,
            settings=settings,
            store=store,
            safety=SafetyEngine(),
            analyser=analyser,
            knowledge=build_dev_knowledge(),
        )
        session = VoiceSession(
            engine=engine,
            audio_input=audio_in,
            audio_output=audio_out,
            vad=vad,
            stt=stt,
            tts=tts,
            config=session_config(settings, greet=False),
            on_event=on_event,
        )
        await session.run()

    stats = session.turn_metrics
    if stats:
        summarise("endpoint latency", [s.endpoint_latency_ms for s in stats])
        summarise("stt", [s.stt_ms for s in stats])
        summarise("engine", [s.engine_ms for s in stats])
        summarise("tts first audio", [s.tts_first_audio_ms for s in stats])
        summarise(
            "SPEECH END -> FIRST AUDIO",
            [s.time_to_first_audio_ms for s in stats],
            target="< 700 ms",
        )
        acked = sum(1 for s in stats if s.acknowledged)
        print(f"  {'turns':<28} {len(stats)}/{len(utterances)}   {DIM}acknowledged {acked}{RESET}")
        if audio_in.timed_out:
            print(f"  {YELLOW}{audio_in.timed_out} turn(s) exceeded the 15 s wait{RESET}")
    else:
        print(f"  {YELLOW}no turns completed — check VAD thresholds{RESET}")

    if llm_provider is not None and hasattr(llm_provider, "aclose"):
        await llm_provider.aclose()
    await store.close()
    await dispose_engine()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
