"""Canonical internal audio format, and the only place conversions happen.

**One format flows through the whole pipeline**: 16 kHz, mono, signed 16-bit
little-endian PCM, in 20 ms frames. Microphone, VAD, STT, TTS and playback all
speak it, so nothing resamples on the hot path.

The constants live in ``app.speech.providers`` (declared in Milestone 1); this
module is the arithmetic and the conversions.

Conversion happens only at the edges where the outside world disagrees:

* **Capture** — devices prefer 48 kHz, so the microphone downsamples once in.
* **TTS output** — Piper emits 22.05 kHz, so synthesis downsamples once out.
* **Telephony (Milestone 4)** — 8 kHz μ-law. ``resample_pcm16`` takes arbitrary
  integer rates precisely so that transport can reuse it unchanged.

Resampling uses ``soxr`` rather than naive decimation: dropping samples aliases
audibly and measurably degrades Whisper accuracy. It costs ~0.24 ms per second
of audio, which is free at this scale.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
import soxr

from app.speech.providers import FRAME_BYTES, FRAME_MS, SAMPLE_RATE_HZ

SAMPLE_WIDTH_BYTES = 2
CHANNELS = 1
FRAME_SAMPLES = SAMPLE_RATE_HZ * FRAME_MS // 1000  # 320
_INT16_FULL_SCALE = 32768.0

#: soxr quality. "HQ" is ~4x faster than "VHQ" and indistinguishable for
#: speech-band downsampling, which is all we do.
_RESAMPLE_QUALITY = "HQ"


@dataclass(frozen=True, slots=True)
class AudioFormat:
    """Describes a PCM stream. Used only to describe the *edges* of the pipeline."""

    sample_rate_hz: int = SAMPLE_RATE_HZ
    channels: int = CHANNELS
    sample_width_bytes: int = SAMPLE_WIDTH_BYTES

    @property
    def bytes_per_second(self) -> int:
        return self.sample_rate_hz * self.channels * self.sample_width_bytes

    def duration_ms(self, pcm: bytes) -> float:
        return len(pcm) / self.bytes_per_second * 1000.0

    def frame_bytes(self, frame_ms: int = FRAME_MS) -> int:
        return self.sample_rate_hz * frame_ms // 1000 * self.channels * self.sample_width_bytes


#: The format every internal component uses.
CANONICAL = AudioFormat()


# --- Conversions ------------------------------------------------------------
def to_int16_array(pcm: bytes) -> np.ndarray:
    """View PCM bytes as int16 samples, dropping an odd trailing byte."""
    return np.frombuffer(pcm[: len(pcm) & ~1], dtype="<i2")


def to_float32(pcm: bytes) -> np.ndarray:
    """PCM bytes to float32 samples normalised to [-1, 1).

    This is what both the VAD and the STT model consume.
    """
    return to_int16_array(pcm).astype(np.float32) / _INT16_FULL_SCALE


def from_float32(samples: np.ndarray) -> bytes:
    """Float samples back to canonical PCM bytes, clipped rather than wrapped.

    Clipping matters: without it a value slightly over 1.0 wraps to a large
    negative int16 and produces an audible click.
    """
    clipped = np.clip(samples, -1.0, 1.0)
    return (clipped * (_INT16_FULL_SCALE - 1)).astype("<i2").tobytes()


def resample_pcm16(pcm: bytes, from_rate: int, to_rate: int, *, channels: int = 1) -> bytes:
    """Resample 16-bit PCM between integer sample rates, downmixing to mono."""
    if not pcm:
        return b""
    samples = to_int16_array(pcm)
    if channels > 1:
        usable = len(samples) - (len(samples) % channels)
        samples = samples[:usable].reshape(-1, channels).mean(axis=1)
    if from_rate == to_rate:
        return samples.astype("<i2").tobytes()
    resampled = soxr.resample(
        samples.astype(np.float32) / _INT16_FULL_SCALE,
        from_rate,
        to_rate,
        quality=_RESAMPLE_QUALITY,
    )
    return from_float32(resampled)


# --- Measurement ------------------------------------------------------------
def duration_ms(pcm: bytes) -> float:
    return CANONICAL.duration_ms(pcm)


def duration_seconds(pcm: bytes) -> float:
    return len(pcm) / CANONICAL.bytes_per_second


def silence(ms: int) -> bytes:
    """``ms`` of canonical-format silence."""
    return b"\x00" * (int(CANONICAL.bytes_per_second * ms / 1000) & ~1)


def rms(pcm: bytes) -> float:
    """Root-mean-square level, normalised to [0, 1]. 0.0 for an empty buffer."""
    samples = to_float32(pcm)
    if samples.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(np.square(samples, dtype=np.float64))))


def peak(pcm: bytes) -> float:
    samples = to_float32(pcm)
    return 0.0 if samples.size == 0 else float(np.abs(samples).max())


def dbfs(pcm: bytes) -> float:
    """Level in dBFS, floored at -100 so it is safe to log or compare."""
    level = rms(pcm)
    return -100.0 if level <= 1e-9 else 20.0 * math.log10(level)


# --- Framing ----------------------------------------------------------------
def iter_frames(pcm: bytes, frame_bytes: int = FRAME_BYTES) -> list[bytes]:
    """Split PCM into fixed-size frames, discarding a trailing partial frame.

    VAD models require exact frame sizes; a short final frame would either
    raise or be silently zero-padded into a false negative.
    """
    if frame_bytes <= 0:
        raise ValueError("frame_bytes must be positive")
    return [pcm[i : i + frame_bytes] for i in range(0, len(pcm) - frame_bytes + 1, frame_bytes)]


def pad_to_frame(pcm: bytes, frame_bytes: int = FRAME_BYTES) -> bytes:
    """Zero-pad the tail so the buffer is a whole number of frames."""
    remainder = len(pcm) % frame_bytes
    return pcm if remainder == 0 else pcm + b"\x00" * (frame_bytes - remainder)


# --- WAV (developer tooling only) -------------------------------------------
def wav_bytes(pcm: bytes, sample_rate_hz: int = SAMPLE_RATE_HZ) -> bytes:
    """Wrap canonical PCM in a WAV container, in memory.

    Benchmarks and fixtures only. Nothing in the runtime pipeline writes audio
    to disk — see docs/privacy/data-inventory.md.
    """
    import io
    import wave

    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(CHANNELS)
        handle.setsampwidth(SAMPLE_WIDTH_BYTES)
        handle.setframerate(sample_rate_hz)
        handle.writeframes(pcm)
    return buffer.getvalue()


def read_wav(data: bytes) -> tuple[bytes, int]:
    """Read a 16-bit WAV into (pcm, sample_rate). Developer tooling only."""
    import io
    import wave

    with wave.open(io.BytesIO(data), "rb") as handle:
        if handle.getsampwidth() != SAMPLE_WIDTH_BYTES:
            raise ValueError("only 16-bit PCM WAV is supported")
        rate = handle.getframerate()
        channels = handle.getnchannels()
        pcm = handle.readframes(handle.getnframes())
    if channels > 1:
        pcm = resample_pcm16(pcm, rate, rate, channels=channels)
    return pcm, rate
