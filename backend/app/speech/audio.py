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


# --- Telephone codecs (G.711) -----------------------------------------------
# A phone leg is 8 kHz G.711, and every conversion between that and the
# canonical format happens here — the same rule that already applies to
# resampling. A μ-law decoder that grows a second home in the telephony
# adapter is how two subtly different decoders end up in one codebase.
#
# Tables are built once on first use rather than at import, so the text-only
# deployment does not pay for a codec it never runs.
_ULAW_BIAS = 0x84
_ULAW_CLIP = 32635
_ALAW_CLIP = 32635
_ULAW_DECODE: np.ndarray | None = None
_ULAW_ENCODE: np.ndarray | None = None
_ALAW_DECODE: np.ndarray | None = None
_ALAW_ENCODE: np.ndarray | None = None


def _build_ulaw_tables() -> tuple[np.ndarray, np.ndarray]:
    codes = np.arange(256, dtype=np.int32)
    inverted = ~codes & 0xFF
    mantissa = inverted & 0x0F
    exponent = (inverted >> 4) & 0x07
    magnitude = (((mantissa << 3) + _ULAW_BIAS) << exponent) - _ULAW_BIAS
    decode = np.where(inverted & 0x80, -magnitude, magnitude).astype(np.int16)

    # Encode by exhaustive table over every int16 value (64 KB). Exact by
    # construction and a single numpy index at call time.
    samples = np.arange(-32768, 32768, dtype=np.int32)
    sign = np.where(samples < 0, 0x80, 0x00)
    magnitude = np.minimum(np.abs(samples), _ULAW_CLIP) + _ULAW_BIAS
    exponent = np.clip(np.floor(np.log2(magnitude)).astype(np.int32) - 7, 0, 7)
    mantissa = (magnitude >> (exponent + 3)) & 0x0F
    encode = (~(sign | (exponent << 4) | mantissa) & 0xFF).astype(np.uint8)
    return decode, encode


def _build_alaw_tables() -> tuple[np.ndarray, np.ndarray]:
    codes = np.arange(256, dtype=np.int32)
    toggled = codes ^ 0x55
    mantissa = toggled & 0x0F
    exponent = (toggled >> 4) & 0x07
    magnitude = np.where(
        exponent == 0,
        (mantissa << 4) + 8,
        ((mantissa << 4) + 0x108) << (exponent - 1),
    )
    # In A-law the sign bit is set for *positive* values — the opposite of
    # μ-law's inverted convention. Getting this backwards still round-trips
    # byte-for-byte, so it only shows up as inverted-phase audio: measured
    # -11.5 dB SNR against the source sine.
    decode = np.where(toggled & 0x80, magnitude, -magnitude).astype(np.int16)

    # Follows the ITU-T G.711 reference encoder. Three details are easy to get
    # wrong and none of them break the round trip loudly:
    #   * A-law quantises in the 13-bit domain, so the input is shifted first;
    #   * negatives fold as ``-v - 1``, not ``abs(v)``;
    #   * segments 0 and 1 share a mantissa shift, unlike every later segment.
    samples = np.arange(-32768, 32768, dtype=np.int32)
    folded = samples >> 3
    mask = np.where(folded >= 0, 0xD5, 0x55)
    folded = np.where(folded >= 0, folded, -folded - 1)

    segment_ends = np.array([0x1F, 0x3F, 0x7F, 0xFF, 0x1FF, 0x3FF, 0x7FF, 0xFFF], dtype=np.int32)
    segment = np.searchsorted(segment_ends, folded, side="left").astype(np.int32)
    shift = np.where(segment < 2, 1, segment)
    encode = (((segment << 4) | ((folded >> shift) & 0x0F)) ^ mask).astype(np.uint8)
    return decode, encode


def ulaw_to_pcm16(data: bytes) -> bytes:
    """Decode G.711 μ-law to 16-bit linear PCM at the *same* sample rate."""
    global _ULAW_DECODE, _ULAW_ENCODE
    if _ULAW_DECODE is None:
        _ULAW_DECODE, _ULAW_ENCODE = _build_ulaw_tables()
    return _ULAW_DECODE[np.frombuffer(data, dtype=np.uint8)].tobytes()


def pcm16_to_ulaw(pcm: bytes) -> bytes:
    """Encode 16-bit linear PCM to G.711 μ-law at the *same* sample rate."""
    global _ULAW_DECODE, _ULAW_ENCODE
    if _ULAW_ENCODE is None:
        _ULAW_DECODE, _ULAW_ENCODE = _build_ulaw_tables()
    return _ULAW_ENCODE[to_int16_array(pcm).astype(np.int32) + 32768].tobytes()


def alaw_to_pcm16(data: bytes) -> bytes:
    """Decode G.711 A-law to 16-bit linear PCM at the *same* sample rate."""
    global _ALAW_DECODE, _ALAW_ENCODE
    if _ALAW_DECODE is None:
        _ALAW_DECODE, _ALAW_ENCODE = _build_alaw_tables()
    return _ALAW_DECODE[np.frombuffer(data, dtype=np.uint8)].tobytes()


def pcm16_to_alaw(pcm: bytes) -> bytes:
    """Encode 16-bit linear PCM to G.711 A-law at the *same* sample rate."""
    global _ALAW_DECODE, _ALAW_ENCODE
    if _ALAW_ENCODE is None:
        _ALAW_DECODE, _ALAW_ENCODE = _build_alaw_tables()
    return _ALAW_ENCODE[to_int16_array(pcm).astype(np.int32) + 32768].tobytes()


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
