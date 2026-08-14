"""Speech provider interfaces: VAD, STT and TTS.

STATUS: interfaces only. **Nothing here is implemented** — that is Milestone 3.
All three providers default to ``none`` in configuration, and ``GET /health``
reports them as ``not_configured`` rather than pretending they work.

They are declared now because the streaming and barge-in design depends on
their shape, and because the conversation engine must not have to change when
they arrive: the engine already speaks text in and text out.

Latency budget the implementations must fit into (docs/architecture/latency.md):

    caller stops speaking  →  VAD endpoint      ~50 ms
    endpoint               →  final transcript  ~300 ms
    transcript             →  intent            ~100 ms  (baseline: <5 ms)
    intent                 →  tool result       ~150 ms
    tool result            →  first TTS audio   ~200 ms
                                                -------
                                                ~800 ms to first audio
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, runtime_checkable

#: Telephony-native format. Resampling anywhere else in the pipeline wastes
#: latency, so providers are expected to accept and emit this directly.
SAMPLE_RATE_HZ = 16_000
FRAME_MS = 20
FRAME_BYTES = SAMPLE_RATE_HZ // 1000 * FRAME_MS * 2  # 16-bit mono


class SpeechEvent(StrEnum):
    SPEECH_STARTED = "SPEECH_STARTED"
    SPEECH_ENDED = "SPEECH_ENDED"
    SILENCE = "SILENCE"
    #: Caller started speaking while the bot was talking. The pipeline must
    #: cancel playback on this event, not after the current phrase finishes.
    BARGE_IN = "BARGE_IN"


@dataclass(frozen=True, slots=True)
class Transcript:
    text: str
    #: Partial transcripts let intent detection start before the caller stops.
    is_final: bool
    confidence: float = 0.0
    #: Milliseconds from speech end to this result. Recorded as a metric.
    latency_ms: float = 0.0


@runtime_checkable
class VoiceActivityDetector(Protocol):
    """Frame-level speech detection (Silero or WebRTC VAD).

    Independent of STT by design: barge-in must be detected from raw audio in
    tens of milliseconds, long before any transcription exists.
    """

    def process_frame(self, frame: bytes) -> SpeechEvent | None:
        """Classify one ``FRAME_BYTES`` frame. Must be non-blocking."""
        ...

    def reset(self) -> None: ...


@runtime_checkable
class SpeechToTextProvider(Protocol):
    """Local STT (whisper.cpp or faster-whisper).

    Raw audio is not retained: implementations consume the stream and discard
    it. ``RETAIN_AUDIO`` exists only as an explicit, governance-gated override.
    """

    async def transcribe(self, audio: bytes) -> Transcript:
        """Transcribe a complete utterance."""
        ...

    # `def`, not `async def`, for the same reason as synthesize_stream below:
    # the implementation is an async generator, so calling it returns the
    # iterator rather than a coroutine to await.
    def transcribe_stream(self, frames: AsyncIterator[bytes]) -> AsyncIterator[Transcript]:
        """Yield partial transcripts, then a final one."""
        ...

    async def health(self) -> bool: ...


@runtime_checkable
class TextToSpeechProvider(Protocol):
    """Local TTS (Piper).

    ``synthesize_stream`` must yield the first chunk as soon as it exists
    rather than after the whole utterance, and must stop promptly when the
    consumer stops iterating — that cancellation is what makes barge-in feel
    immediate.
    """

    async def synthesize(self, text: str) -> bytes: ...

    # Deliberately `def`, not `async def`. Every implementation is an async
    # generator, and calling one returns the iterator immediately rather than a
    # coroutine that must be awaited first — which is exactly what `def ... ->
    # AsyncIterator[bytes]` describes. Declared `async def`, this said callers
    # must write `async for c in await tts.synthesize_stream(t)`, which is not
    # what any of them do and would fail at runtime.
    def synthesize_stream(self, text: str) -> AsyncIterator[bytes]: ...

    async def warm_cache(self, phrases: tuple[str, ...]) -> None:
        """Pre-render fixed phrases (``responses.PREGENERATED``) so the common
        turns play with no synthesis latency at all."""
        ...

    async def health(self) -> bool: ...
