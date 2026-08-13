"""Local speech-to-text via faster-whisper (CTranslate2).

Chosen over whisper.cpp and mlx-whisper for this hardware after measurement —
see docs/architecture/voice-pipeline.md. Runs int8 on CPU, so it leaves the GPU
and most of the 8 GB free for Ollama.

Two things matter for a phone agent and are handled here:

* **Blocking inference must leave the event loop.** Transcription is a
  CPU-bound C++ call; running it inline would stall audio capture and make
  barge-in detection late. It runs in a worker thread.
* **A bounded worker pool.** Concurrent calls would otherwise each spawn a
  thread and thrash 8 GB. A semaphore caps them, matching the LLM provider's
  approach.

``transcribe_stream`` yields partial transcripts. They are advisory only —
``app.voice.session`` never sends a partial to the ConversationEngine as a
caller turn.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator
from typing import Any

from app.core.logging import get_logger
from app.core.metrics import M, metrics
from app.speech.audio import duration_seconds, to_float32
from app.speech.providers import Transcript

logger = get_logger(__name__)

#: Refuse anything longer than this outright. Bounds both memory and latency
#: from a stuck microphone or a hostile caller holding the line open.
MAX_UTTERANCE_SECONDS = 30.0

#: Whisper hallucinates confident text on silence — famously "Thank you." or
#: subtitle credits. Anything matching this on a near-silent clip is discarded
#: rather than sent to the engine as a caller turn.
_SILENCE_HALLUCINATIONS = frozenset(
    {
        "thank you.",
        "thanks for watching!",
        "you",
        "thank you for watching.",
        "bye.",
        ".",
        "...",
        "[blank_audio]",
        "(silence)",
    }
)


class FasterWhisperSTT:
    """``SpeechToTextProvider`` backed by faster-whisper."""

    def __init__(
        self,
        *,
        model: str = "base.en",
        device: str = "cpu",
        compute_type: str = "int8",
        language: str = "en",
        beam_size: int = 1,
        max_concurrency: int = 1,
        initial_prompt: str | None = None,
        download_root: str | None = None,
    ) -> None:
        self._model_name = model
        self._device = device
        self._compute_type = compute_type
        self._language = language
        # Greedy decoding: beam search roughly doubles latency for a marginal
        # accuracy gain on short utterances, which is the wrong trade here.
        self._beam_size = beam_size
        self._initial_prompt = initial_prompt
        self._download_root = download_root
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._model: Any | None = None
        self._load_lock = asyncio.Lock()

    @property
    def name(self) -> str:
        return "faster_whisper"

    @property
    def model_name(self) -> str:
        return self._model_name

    def set_vocabulary(self, terms: tuple[str, ...]) -> None:
        """Bias recognition toward clinic vocabulary.

        Only clinic-wide terms — practitioner display names, appointment type
        names, the clinic name. Never patient data: the prompt is sent to the
        decoder on every utterance, so patient names in it would leak across
        callers as well as being a privacy breach.
        """
        if not terms:
            return
        self._initial_prompt = "Riverside Medical Clinic. " + ", ".join(terms) + "."

    async def _ensure_model(self) -> Any:
        """Load once, lazily, under a lock so concurrent turns share the load."""
        if self._model is not None:
            return self._model
        async with self._load_lock:
            if self._model is not None:
                return self._model
            from faster_whisper import WhisperModel

            # faster-whisper writes a per-utterance line through the stdlib
            # root logger, bypassing our structured, redaction-aware handlers.
            # Warnings and errors still surface; the running commentary does not.
            logging.getLogger("faster_whisper").setLevel(logging.WARNING)

            started = time.perf_counter()
            self._model = await asyncio.to_thread(
                WhisperModel,
                self._model_name,
                device=self._device,
                compute_type=self._compute_type,
                download_root=self._download_root,
            )
            logger.info(
                "stt.model_loaded",
                provider=self.name,
                model=self._model_name,
                duration_ms=round((time.perf_counter() - started) * 1000),
            )
        return self._model

    async def transcribe(self, audio: bytes) -> Transcript:
        """Transcribe a complete utterance of canonical PCM."""
        seconds = duration_seconds(audio)
        if seconds <= 0:
            return Transcript(text="", is_final=True)
        if seconds > MAX_UTTERANCE_SECONDS:
            # Truncate rather than refuse: the caller said *something*, and the
            # first 30 s is far more likely to carry the request than the tail.
            logger.info("stt.utterance_truncated", seconds=round(seconds, 1))
            audio = audio[: int(MAX_UTTERANCE_SECONDS * 32_000)]
            seconds = MAX_UTTERANCE_SECONDS

        model = await self._ensure_model()
        samples = to_float32(audio)
        started = time.perf_counter()
        async with self._semaphore:
            text, confidence = await asyncio.to_thread(self._run, model, samples)
        elapsed_ms = (time.perf_counter() - started) * 1000

        metrics.observe(M.STT_LATENCY, elapsed_ms, labels={"model": self._model_name})
        metrics.observe(M.STT_REAL_TIME_FACTOR, elapsed_ms / max(seconds * 1000, 1e-6) * 100)
        # Text is never logged — only its length.
        logger.debug(
            "stt.transcribed",
            provider=self.name,
            duration_ms=round(elapsed_ms),
            audio_ms=round(seconds * 1000),
            chars=len(text),
        )
        return Transcript(text=text, is_final=True, confidence=confidence, latency_ms=elapsed_ms)

    def _run(self, model: Any, samples: Any) -> tuple[str, float]:
        segments, info = model.transcribe(
            samples,
            language=self._language,
            beam_size=self._beam_size,
            vad_filter=False,  # our own VAD already segmented the utterance
            condition_on_previous_text=False,  # no cross-turn context bleed
            initial_prompt=self._initial_prompt,
            # A single temperature, which disables the fallback retry loop.
            # By default faster-whisper re-decodes at rising temperatures when a
            # result trips its compression/logprob thresholds, and short
            # utterances trip them constantly: measured 1602 ms for "Hughes" and
            # 1671 ms for "Nguyen" against ~370 ms with the retries off — for
            # *identical* transcripts in every case. Single-word answers (a
            # surname, "yes", an appointment type) are the most common turn in a
            # clinic call, so this is the difference between a 400 ms and a
            # 1.6 s reply on the turns that happen most.
            #
            # The trade: a degenerate decode is no longer retried. Acceptable
            # here because our VAD has already segmented the audio and
            # MAX_UTTERANCE_SECONDS bounds its length, so the pathological
            # long-rambling input the fallback protects against cannot reach it.
            temperature=0.0,
        )
        collected = list(segments)
        text = " ".join(segment.text for segment in collected).strip()
        if not collected:
            return "", 0.0
        # avg_logprob is per-segment; exp() gives a rough 0-1 confidence.
        mean_logprob = sum(s.avg_logprob for s in collected) / len(collected)
        confidence = float(min(1.0, max(0.0, 2.718281828**mean_logprob)))
        del info
        return text, confidence

    async def transcribe_stream(self, frames: AsyncIterator[bytes]) -> AsyncIterator[Transcript]:
        """Accumulate frames and yield a single final transcript.

        faster-whisper has no true streaming decoder. Re-running the model on a
        growing buffer would produce partials, but at ~400 ms each it would
        saturate the CPU that the final transcript needs, and it would delay
        the only result that is allowed to drive a turn. So this yields one
        final result, and the orchestrator does not depend on partials.
        """
        buffer = bytearray()
        async for frame in frames:
            buffer.extend(frame)
        yield await self.transcribe(bytes(buffer))

    async def health(self) -> bool:
        try:
            await self._ensure_model()
        except Exception as exc:
            logger.warning("stt.health_failed", provider=self.name, error=type(exc).__name__)
            return False
        return True

    async def aclose(self) -> None:
        self._model = None


def looks_like_silence_hallucination(
    text: str, audio: bytes, *, rms_threshold: float = 0.01
) -> bool:
    """Whether a transcript is Whisper inventing words over near-silence.

    Checked by the orchestrator before a transcript becomes a caller turn: a
    hallucinated "Thank you." would otherwise be a real conversational turn,
    and on a confirmation state it could read as agreement.
    """
    from app.speech.audio import rms

    if not text:
        return False
    return text.strip().lower() in _SILENCE_HALLUCINATIONS and rms(audio) < rms_threshold
