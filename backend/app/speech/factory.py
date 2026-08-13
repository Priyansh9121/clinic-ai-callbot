"""Construction of speech providers from configuration.

The only place a configured provider name maps to a concrete engine. Nothing in
``app.voice`` imports faster-whisper, Piper or onnxruntime — swapping an engine
means adding a branch here.

Construction is deliberately cheap and offline: models load lazily on first use,
so building a provider never blocks startup or requires the model to be present.
A missing model surfaces through ``health()``, not as an import error.
"""

from __future__ import annotations

from pathlib import Path

from app.core.config import Settings
from app.core.logging import get_logger
from app.speech.providers import (
    SpeechToTextProvider,
    TextToSpeechProvider,
    VoiceActivityDetector,
)

logger = get_logger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[3]


def _resolve(path: str) -> Path:
    """Resolve a configured model path relative to the repository root."""
    candidate = Path(path)
    return candidate if candidate.is_absolute() else REPO_ROOT / candidate


def build_vad(settings: Settings) -> VoiceActivityDetector | None:
    if settings.vad_provider == "none":
        return None
    if settings.vad_provider == "energy":
        from app.speech.vad.energy import EnergyVAD

        return EnergyVAD()
    if settings.vad_provider == "silero":
        from app.speech.vad.silero import SileroVAD

        model = _resolve(settings.vad_model)
        try:
            return SileroVAD(model, threshold=settings.vad_threshold)
        except FileNotFoundError:
            # Degrade rather than refuse to run: an energy VAD still segments
            # speech, and the CLI reports which one is active.
            logger.warning(
                "vad.model_missing",
                path=str(model),
                detail="falling back to the energy VAD",
            )
            from app.speech.vad.energy import EnergyVAD

            return EnergyVAD()
    return None


def build_stt(settings: Settings) -> SpeechToTextProvider | None:
    if settings.stt_provider == "none":
        return None
    if settings.stt_provider == "faster_whisper":
        from app.speech.stt.whisper import FasterWhisperSTT

        return FasterWhisperSTT(
            model=settings.stt_model,
            compute_type=settings.stt_compute_type,
            language=settings.stt_language,
            beam_size=settings.stt_beam_size,
        )
    return None


def build_tts(settings: Settings) -> TextToSpeechProvider | None:
    """Build the TTS engine wrapped in the approved-phrase cache."""
    if settings.tts_provider == "none":
        return None
    if settings.tts_provider == "piper":
        from app.speech.tts.cache import CachingTTS
        from app.speech.tts.piper import PiperTTS

        engine = PiperTTS(
            _resolve(settings.tts_voice),
            length_scale=settings.tts_length_scale,
        )
        return CachingTTS(engine, max_entries=settings.tts_cache_entries)
    return None
