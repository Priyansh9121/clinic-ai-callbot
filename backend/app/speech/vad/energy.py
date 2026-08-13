"""Energy-threshold VAD — dependency-free fallback and test double.

Not as good as Silero: it cannot distinguish speech from a slammed door, and a
noisy room forces the threshold up until quiet speech is missed. It exists
because it is genuinely useful in two places:

* **Tests.** Deterministic, instant, and needs no model file — so orchestration
  tests can exercise real VAD logic offline.
* **Degradation.** If the Silero model is missing, the pipeline still detects
  speech rather than refusing to run.

Hysteresis (separate start/stop thresholds) is what stops a level hovering on
the boundary from producing a burst of start/stop events.
"""

from __future__ import annotations

from app.speech.audio import dbfs
from app.speech.providers import SpeechEvent


class EnergyVAD:
    """Frame-level speech detection by RMS level in dBFS.

    Implements ``VoiceActivityDetector``. Emits an event only on a *transition*,
    so the caller can treat any non-None return as a change of state.
    """

    def __init__(
        self,
        *,
        start_dbfs: float = -40.0,
        stop_dbfs: float = -45.0,
        start_frames: int = 2,
        stop_frames: int = 10,
    ) -> None:
        if stop_dbfs > start_dbfs:
            raise ValueError("stop_dbfs must be at or below start_dbfs (hysteresis)")
        self._start_dbfs = start_dbfs
        self._stop_dbfs = stop_dbfs
        self._start_frames = max(1, start_frames)
        self._stop_frames = max(1, stop_frames)
        self._speaking = False
        self._above = 0
        self._below = 0

    @property
    def is_speaking(self) -> bool:
        return self._speaking

    def process_frame(self, frame: bytes) -> SpeechEvent | None:
        level = dbfs(frame)
        if not self._speaking:
            self._above = self._above + 1 if level >= self._start_dbfs else 0
            if self._above >= self._start_frames:
                self._speaking = True
                self._below = 0
                return SpeechEvent.SPEECH_STARTED
            return None

        self._below = self._below + 1 if level < self._stop_dbfs else 0
        if self._below >= self._stop_frames:
            self._speaking = False
            self._above = 0
            return SpeechEvent.SPEECH_ENDED
        return None

    def reset(self) -> None:
        self._speaking = False
        self._above = 0
        self._below = 0
