"""Semantic end-of-turn detection, behind a Protocol like every other engine.

The silence rule answers "has this person stopped making noise?". That is not
the question. A caller spelling a surname stops making noise six times, and the
measured cost of treating each pause as a turn is in
``scripts/benchmark_voice.py --sweep-endpointing``: at 500 ms it splits a
spelled "H-U-G-H-E-S" five times and cuts a date of birth in half.

A turn detector answers the question directly — given the audio so far, is this
person finished? — so a short pause inside a spelled surname and the same pause
at the end of a sentence can be told apart.

**It may only ever end a turn early.** The silence rule still runs and still
fires at ``end_silence_ms`` whatever the model says, so the model can shorten a
turn and never extend one. That bounds the damage a wrong answer can do to
latency, and means a model that is unavailable, slow or absent degrades to
exactly the behaviour that shipped before it.

Smart Turn v3 (pipecat-ai, BSD-2-Clause) takes an 8-second 80-bin log-mel
window and returns one logit. Measured on this machine: 21 ms inference at four
threads plus 1.5 ms of feature extraction, which is affordable against the
466 ms it is trying to save.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from app.core.logging import get_logger
from app.speech.providers import SAMPLE_RATE_HZ

logger = get_logger(__name__)

#: The model's input window: 80 mel bins x 800 frames at a 10 ms hop.
MEL_BINS = 80
MEL_FRAMES = 800
WINDOW_SECONDS = 8


@runtime_checkable
class TurnDetector(Protocol):
    """Given the utterance so far, is the caller finished?"""

    @property
    def name(self) -> str: ...

    def is_complete(self, pcm: bytes) -> float:
        """Return P(turn complete) in 0..1 for canonical 16 kHz mono PCM."""
        ...


class SmartTurnV3Detector:
    """Smart Turn v3 over onnxruntime — the same runtime Silero already uses.

    Loads lazily and fails soft: a missing file, a corrupt model or a bad frame
    returns 0.0, which means "not finished" and leaves the silence rule to make
    the decision. Being unavailable must cost latency, never a cut-off caller.
    """

    def __init__(self, model_path: str | Path, *, threads: int = 4) -> None:
        self._model_path = Path(model_path)
        self._threads = threads
        self._session: Any | None = None
        self._features: Any | None = None
        self._unavailable = False

    @property
    def name(self) -> str:
        return "smart_turn_v3"

    def _ensure_loaded(self) -> bool:
        if self._session is not None:
            return True
        if self._unavailable:
            return False
        try:
            import numpy as np
            import onnxruntime as ort
            from faster_whisper.feature_extractor import FeatureExtractor

            options = ort.SessionOptions()
            options.intra_op_num_threads = self._threads
            self._session = ort.InferenceSession(
                str(self._model_path), options, providers=["CPUExecutionProvider"]
            )
            self._features = FeatureExtractor(feature_size=MEL_BINS)
            self._np = np
        except Exception as exc:
            # Absent or unloadable is a supported state, not an error: the
            # silence rule is a complete endpointer on its own.
            self._unavailable = True
            logger.warning(
                "voice.turn_detector_unavailable",
                model=str(self._model_path),
                error=type(exc).__name__,
            )
            return False
        logger.info("voice.turn_detector_loaded", model=self.name, threads=self._threads)
        return True

    def is_complete(self, pcm: bytes) -> float:
        if not self._ensure_loaded():
            return 0.0
        session, features = self._session, self._features
        if session is None or features is None:  # pragma: no cover - guarded above
            return 0.0
        try:
            np = self._np
            audio = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0
            # The model wants the *end* of the utterance: the decision is about
            # how it finished, and an 8 s window starting at the beginning of a
            # long answer would not contain it.
            window = SAMPLE_RATE_HZ * WINDOW_SECONDS
            audio = audio[-window:]
            if audio.size < window:
                audio = np.pad(audio, (window - audio.size, 0))
            # Zero-mean unit-variance on the waveform, which is what the
            # reference gets from WhisperFeatureExtractor(do_normalize=True).
            # faster-whisper's extractor does not do this, and without it the
            # model sees an input scale it was never trained on.
            audio = (audio - audio.mean()) / np.sqrt(audio.var() + 1e-7)
            mel = np.asarray(features(audio), dtype=np.float32)[:, :MEL_FRAMES]
            if mel.shape != (MEL_BINS, MEL_FRAMES):
                return 0.0
            # The ONNX graph ends in a sigmoid: this is already P(complete).
            # Applying another one squashed every input into [0.5, 0.731] and
            # made a working model look like it could not discriminate.
            return float(session.run(None, {"input_features": mel[None, ...]})[0].reshape(-1)[0])
        except Exception as exc:
            logger.warning("voice.turn_detector_failed", error=type(exc).__name__)
            return 0.0


def build_turn_detector(settings: Any) -> TurnDetector | None:
    """Resolve the configured detector, or None for the silence rule alone."""
    provider = getattr(settings, "voice_turn_detector", "none")
    if provider != "smart_turn_v3":
        return None
    path = getattr(settings, "voice_turn_detector_model", "")
    if not path:
        logger.warning("voice.turn_detector_unconfigured", reason="no model path")
        return None
    resolved = Path(path)
    if not resolved.is_absolute():
        from app.core.config import REPO_ROOT

        resolved = REPO_ROOT / resolved
    return SmartTurnV3Detector(resolved)
