"""Test doubles for the voice pipeline.

Ordinary ``pytest`` must never need a microphone, a speaker, a Whisper model or
a Piper voice, so every orchestration test uses these. Live-engine checks are
marked ``voice_live`` and skipped by default.

The fakes are deliberately *behavioural*, not trivial: ``FakeAudioOutput``
records what was actually queued versus discarded, which is what the barge-in
and stale-turn tests assert on.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

from app.speech.audio import silence
from app.speech.providers import FRAME_BYTES, SpeechEvent, Transcript


@dataclass
class ScriptedFrame:
    """One 20 ms frame of the scripted stream."""

    pcm: bytes
    is_speech: bool


def speech_frames(count: int) -> list[ScriptedFrame]:
    """``count`` frames that the fake VAD will treat as speech."""
    return [ScriptedFrame(b"\x10\x10" * (FRAME_BYTES // 2), True) for _ in range(count)]


def silent_frames(count: int) -> list[ScriptedFrame]:
    return [ScriptedFrame(silence(20), False) for _ in range(count)]


class FakeAudioInput:
    """Replays a scripted frame sequence, then optionally blocks.

    ``hold_open`` keeps the session alive after the script so a test can drive
    additional behaviour; otherwise the stream ends and the session shuts down.
    """

    def __init__(
        self, script: list[ScriptedFrame] | None = None, *, hold_open: bool = False
    ) -> None:
        self.script = list(script or [])
        self._hold_open = hold_open
        self._running = False
        self.started = False
        self.stopped = False
        #: Frames appended here are emitted after the initial script.
        self.live: asyncio.Queue[ScriptedFrame | None] = asyncio.Queue()

    @property
    def is_running(self) -> bool:
        return self._running

    async def start(self) -> None:
        self._running = True
        self.started = True

    async def frames(self) -> AsyncIterator[bytes]:
        for frame in self.script:
            if not self._running:
                return
            yield frame.pcm
            # Yield control so the session's tasks can interleave, which is
            # what makes barge-in timing testable at all.
            await asyncio.sleep(0)
        while self._running and self._hold_open:
            # Polled rather than blocking forever, mirroring the real
            # microphone's timeout: a stop request must end the stream even if
            # no further audio ever arrives.
            try:
                item = await asyncio.wait_for(self.live.get(), timeout=0.05)
            except TimeoutError:
                continue
            if item is None:
                return
            yield item.pcm

    async def push(self, frame: ScriptedFrame) -> None:
        await self.live.put(frame)

    async def stop(self) -> None:
        self._running = False
        self.stopped = True
        await self.live.put(None)


class FakeAudioOutput:
    """Records queued audio and honours immediate cancellation."""

    def __init__(self) -> None:
        self._running = False
        self._playing = False
        self.started = False
        self.stopped = False
        #: Every chunk that reached the device, in order.
        self.played: list[bytes] = []
        #: Bytes discarded by ``stop_immediately`` — i.e. barge-in.
        self.discarded_bytes = 0
        self.stop_immediately_calls = 0
        self._queued = bytearray()

    @property
    def is_playing(self) -> bool:
        return self._playing

    @property
    def total_bytes(self) -> int:
        return sum(len(chunk) for chunk in self.played)

    async def start(self) -> None:
        self._running = True
        self.started = True

    async def play(self, pcm: bytes) -> None:
        if not self._running or not pcm:
            return
        self._playing = True
        self.played.append(pcm)
        self._queued.extend(pcm)

    async def wait_until_drained(self) -> None:
        self._queued.clear()
        self._playing = False

    async def stop_immediately(self) -> None:
        self.stop_immediately_calls += 1
        self.discarded_bytes += len(self._queued)
        self._queued.clear()
        self._playing = False

    async def stop(self) -> None:
        await self.stop_immediately()
        self._running = False
        self.stopped = True


class FakeVAD:
    """Marks frames as speech based on a non-silent payload.

    Mirrors the real implementations' contract: emits an event only on a
    transition, and exposes ``is_speaking``.
    """

    def __init__(self, *, start_frames: int = 1, stop_frames: int = 1) -> None:
        self._start_frames = start_frames
        self._stop_frames = stop_frames
        self._speaking = False
        self._above = 0
        self._below = 0

    @property
    def is_speaking(self) -> bool:
        return self._speaking

    def process_frame(self, frame: bytes) -> SpeechEvent | None:
        loud = any(frame)
        if not self._speaking:
            self._above = self._above + 1 if loud else 0
            if self._above >= self._start_frames:
                self._speaking = True
                self._below = 0
                return SpeechEvent.SPEECH_STARTED
            return None
        self._below = self._below + 1 if not loud else 0
        if self._below >= self._stop_frames:
            self._speaking = False
            self._above = 0
            return SpeechEvent.SPEECH_ENDED
        return None

    def reset(self) -> None:
        self._speaking = False
        self._above = 0
        self._below = 0


@dataclass
class FakeSTT:
    """Returns scripted transcripts, optionally slowly."""

    transcripts: list[str] = field(default_factory=list)
    delay_seconds: float = 0.0
    calls: int = 0
    #: Set to raise instead of transcribing.
    error: Exception | None = None

    async def transcribe(self, audio: bytes) -> Transcript:
        self.calls += 1
        if self.delay_seconds:
            await asyncio.sleep(self.delay_seconds)
        if self.error is not None:
            raise self.error
        if not self.transcripts:
            return Transcript(text="", is_final=True)
        text = self.transcripts.pop(0) if len(self.transcripts) > 1 else self.transcripts[0]
        return Transcript(text=text, is_final=True, confidence=0.9, latency_ms=1.0)

    async def transcribe_stream(self, frames: AsyncIterator[bytes]) -> AsyncIterator[Transcript]:
        buffer = bytearray()
        async for frame in frames:
            buffer.extend(frame)
        yield await self.transcribe(bytes(buffer))

    async def health(self) -> bool:
        return self.error is None


@dataclass
class FakeTTS:
    """Synthesises deterministic audio, one chunk per sentence."""

    delay_seconds: float = 0.0
    #: Every text passed to synthesis, so tests can assert what was spoken.
    spoken: list[str] = field(default_factory=list)
    chunk_delay_seconds: float = 0.0
    error: Exception | None = None

    async def synthesize(self, text: str) -> bytes:
        return b"".join([chunk async for chunk in self.synthesize_stream(text)])

    async def synthesize_stream(self, text: str) -> AsyncIterator[bytes]:
        if not text:
            return
        self.spoken.append(text)
        if self.error is not None:
            raise self.error
        if self.delay_seconds:
            await asyncio.sleep(self.delay_seconds)
        # One chunk per sentence, mirroring Piper.
        sentences = [s for s in text.split(". ") if s] or [text]
        for sentence in sentences:
            if self.chunk_delay_seconds:
                await asyncio.sleep(self.chunk_delay_seconds)
            yield b"\x20\x20" * (FRAME_BYTES // 2) * max(1, len(sentence) // 20)

    async def warm_cache(self, phrases: tuple[str, ...]) -> None:
        return None

    async def health(self) -> bool:
        return self.error is None
