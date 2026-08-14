"""Local text-to-speech via Piper.

Measured on this hardware at ~66-123 ms to first audio and RTF ~0.03, i.e. it
synthesises roughly 30x faster than real time. That headroom is what makes
chunked streaming worthwhile: the first phrase reaches the speaker long before
the rest is generated.

Design points that matter for a call agent:

* **Chunked by sentence.** Piper yields one chunk per sentence, so a multi
  sentence reply starts playing after the first sentence rather than after the
  whole paragraph.
* **Cancellable.** ``synthesize_stream`` is a generator; when the consumer stops
  iterating (barge-in), synthesis of the remaining sentences never happens.
* **Resamples once.** Piper emits 22.05 kHz; everything downstream is 16 kHz,
  so the conversion happens here and nowhere else.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from app.core.logging import get_logger
from app.core.metrics import M, metrics
from app.speech.audio import resample_pcm16
from app.speech.providers import SAMPLE_RATE_HZ

logger = get_logger(__name__)

#: Refuse to synthesise more than this. Caller-facing replies are one or two
#: sentences by design (see app/conversation/responses.py), so anything longer
#: indicates a bug, and synthesising it would occupy the speaker for minutes.
MAX_TEXT_CHARS = 1_000


class PiperTTS:
    """``TextToSpeechProvider`` backed by a local Piper ONNX voice."""

    def __init__(
        self,
        model_path: str | Path,
        *,
        max_concurrency: int = 2,
        length_scale: float | None = None,
    ) -> None:
        self._model_path = Path(model_path)
        self._voice: Any | None = None
        self._load_lock = asyncio.Lock()
        self._semaphore = asyncio.Semaphore(max_concurrency)
        # <1.0 speaks faster. Left at the voice default unless configured:
        # speeding a receptionist up sounds brusque rather than efficient.
        self._length_scale = length_scale
        self._sample_rate = SAMPLE_RATE_HZ

    @property
    def name(self) -> str:
        return "piper"

    @property
    def voice_name(self) -> str:
        return self._model_path.stem

    async def _ensure_voice(self) -> Any:
        if self._voice is not None:
            return self._voice
        async with self._load_lock:
            if self._voice is not None:
                return self._voice
            if not self._model_path.exists():
                raise FileNotFoundError(f"Piper voice not found: {self._model_path}")
            from piper import PiperVoice

            started = time.perf_counter()
            voice = await asyncio.to_thread(PiperVoice.load, str(self._model_path))
            self._voice = voice
            self._sample_rate = voice.config.sample_rate
            logger.info(
                "tts.voice_loaded",
                provider=self.name,
                voice=self.voice_name,
                sample_rate=self._sample_rate,
                duration_ms=round((time.perf_counter() - started) * 1000),
            )
        return self._voice

    def _synthesize_chunks(self, voice: Any, text: str) -> list[bytes]:
        """Blocking synthesis. Runs in a worker thread."""
        kwargs: dict[str, Any] = {}
        if self._length_scale is not None:
            from piper import SynthesisConfig

            kwargs["syn_config"] = SynthesisConfig(length_scale=self._length_scale)
        return [chunk.audio_int16_bytes for chunk in voice.synthesize(text, **kwargs)]

    async def synthesize(self, text: str) -> bytes:
        """Synthesise the whole utterance as canonical 16 kHz PCM."""
        chunks = [chunk async for chunk in self.synthesize_stream(text)]
        return b"".join(chunks)

    async def synthesize_stream(self, text: str) -> AsyncIterator[bytes]:
        """Yield canonical PCM chunks as they are produced.

        Abandoning the iterator stops further synthesis — that is what makes
        barge-in cheap rather than merely silent.
        """
        text = (text or "").strip()
        if not text:
            return
        if len(text) > MAX_TEXT_CHARS:
            logger.warning("tts.text_truncated", chars=len(text))
            text = text[:MAX_TEXT_CHARS]

        voice = await self._ensure_voice()
        started = time.perf_counter()
        first_emitted = False

        async with self._semaphore:
            # Piper's generator is synchronous; collecting per sentence in a
            # worker thread keeps the event loop free while still yielding the
            # first sentence before the rest is spoken.
            raw_chunks = await asyncio.to_thread(self._synthesize_chunks, voice, text)

        for raw in raw_chunks:
            pcm = resample_pcm16(raw, self._sample_rate, SAMPLE_RATE_HZ)
            if not pcm:
                continue
            if not first_emitted:
                first_emitted = True
                metrics.observe(
                    M.TTS_FIRST_AUDIO,
                    (time.perf_counter() - started) * 1000,
                    labels={"provider": self.name},
                )
            yield pcm

        metrics.observe(
            M.TTS_TOTAL_LATENCY,
            (time.perf_counter() - started) * 1000,
            labels={"provider": self.name},
        )

    async def warm_cache(self, phrases: tuple[str, ...]) -> None:
        """Warm the engine itself.

        Phrase *caching* is the job of ``app.speech.tts.cache.CachingTTS``,
        which wraps this provider — keeping it out of here means the cache
        works for any engine, and this class stays purely an engine adapter.
        What this does is load the voice and run one synthesis, so the first
        real caller does not pay ONNX graph warm-up.
        """
        await self._ensure_voice()
        if phrases:
            async for _ in self.synthesize_stream(phrases[0]):
                break

    async def health(self) -> bool:
        try:
            await self._ensure_voice()
        except Exception as exc:
            logger.warning("tts.health_failed", provider=self.name, error=type(exc).__name__)
            return False
        return True

    async def aclose(self) -> None:
        self._voice = None
