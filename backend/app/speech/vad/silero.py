"""Silero VAD (v5) via onnxruntime.

A 2.3 MB model that scores each frame for speech probability. Far more robust
than an energy threshold: it ignores keyboard clatter, doors and hum that would
otherwise trip barge-in constantly, and it still catches quiet speech.

Three facts drive the implementation:

* **Silero v5 consumes exactly 512 samples (32 ms) at 16 kHz.** Our canonical
  frame is 320 samples (20 ms), so frames are buffered until a full window is
  available. Inference therefore lags the audio by at most one 20 ms frame,
  which is inside the barge-in budget.
* **The 64 samples preceding each window must be prepended**, giving a 576
  sample tensor. This is not optional and not documented on the model itself —
  it lives in Silero's Python wrapper. Feeding a bare 512-sample window makes
  the model return ~0.001 on unmistakable speech, i.e. it silently never fires.
  Measured here: mean probability 0.936 with the context, 0.008 without.
* **It is stateful.** The recurrent state carries between windows and must be
  cleared by ``reset()`` between utterances, or the tail of one utterance
  biases the start of the next.

Inference is a few hundred microseconds per window, so it runs inline on the
event loop rather than in a thread — the hop to a thread would cost more than
the call.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np

from app.core.logging import get_logger
from app.speech.audio import to_float32
from app.speech.providers import SAMPLE_RATE_HZ, SpeechEvent

logger = get_logger(__name__)

#: Fixed by the model architecture — not tunables.
WINDOW_SAMPLES = 512
#: Samples of preceding audio prepended to every window. See module docstring.
CONTEXT_SAMPLES = 64
_STATE_SHAPE = (2, 1, 128)


class SileroVAD:
    """Frame-level speech detection using the Silero v5 ONNX model.

    Implements ``VoiceActivityDetector``. Emits an event only on a transition.
    """

    def __init__(
        self,
        model_path: str | Path,
        *,
        threshold: float = 0.5,
        #: Deliberately lower than ``threshold``: once speech has started, a
        #: brief dip should not end it. Same hysteresis idea as the energy VAD.
        stop_threshold: float = 0.35,
        start_frames: int = 1,
        #: Kept deliberately short (~96 ms). This hangover only has to bridge
        #: the gaps *inside* speech; deciding the turn has ended is the
        #: endpointer's job, and a long hangover here would compound with the
        #: endpointer's silence threshold and make turn-taking sluggish.
        stop_frames: int = 3,
    ) -> None:
        import onnxruntime as ort

        path = Path(model_path)
        if not path.exists():
            raise FileNotFoundError(f"Silero VAD model not found: {path}")

        options = ort.SessionOptions()
        # One thread: this is a tiny model called very often, and thread
        # handoff costs more than the inference itself. It also keeps VAD from
        # competing with Whisper for cores during a turn.
        options.inter_op_num_threads = 1
        options.intra_op_num_threads = 1
        options.log_severity_level = 3
        self._session = ort.InferenceSession(
            str(path), sess_options=options, providers=["CPUExecutionProvider"]
        )

        self._threshold = threshold
        self._stop_threshold = stop_threshold
        self._start_frames = max(1, start_frames)
        self._stop_frames = max(1, stop_frames)

        self._state = np.zeros(_STATE_SHAPE, dtype=np.float32)
        self._sample_rate = np.array(SAMPLE_RATE_HZ, dtype=np.int64)
        self._pending = np.zeros(0, dtype=np.float32)
        self._context = np.zeros(CONTEXT_SAMPLES, dtype=np.float32)
        self._speaking = False
        self._above = 0
        self._below = 0
        self._last_probability = 0.0

    @property
    def is_speaking(self) -> bool:
        return self._speaking

    @property
    def last_probability(self) -> float:
        """Most recent speech probability. Diagnostics and threshold tuning."""
        return self._last_probability

    def process_frame(self, frame: bytes) -> SpeechEvent | None:
        """Feed one canonical 20 ms frame.

        Returns a transition event, or None. Because the model window is 32 ms,
        a single call may run zero or one inferences.
        """
        self._pending = np.concatenate([self._pending, to_float32(frame)])

        event: SpeechEvent | None = None
        while self._pending.size >= WINDOW_SAMPLES:
            window = self._pending[:WINDOW_SAMPLES]
            self._pending = self._pending[WINDOW_SAMPLES:]
            transition = self._infer(window)
            # Keep the first transition in this call: a start immediately
            # followed by a stop within 32 ms is noise, not two turns.
            if transition is not None and event is None:
                event = transition
        return event

    def _infer(self, window: np.ndarray) -> SpeechEvent | None:
        probability, self._state = self._run(window)
        self._last_probability = probability

        if not self._speaking:
            self._above = self._above + 1 if probability >= self._threshold else 0
            if self._above >= self._start_frames:
                self._speaking = True
                self._below = 0
                return SpeechEvent.SPEECH_STARTED
            return None

        self._below = self._below + 1 if probability < self._stop_threshold else 0
        if self._below >= self._stop_frames:
            self._speaking = False
            self._above = 0
            return SpeechEvent.SPEECH_ENDED
        return None

    def _run(self, window: np.ndarray) -> tuple[float, np.ndarray]:
        # The trailing 64 samples of the previous window are prepended; without
        # them the model returns ~0 on any input. The context is then advanced.
        tensor = np.concatenate([self._context, window]).reshape(1, -1)
        outputs = self._session.run(
            None,
            {
                "input": tensor,
                "state": self._state,
                "sr": self._sample_rate,
            },
        )
        self._context = window[-CONTEXT_SAMPLES:]
        return float(outputs[0][0][0]), outputs[1]

    def reset(self) -> None:
        """Clear recurrent state and buffers. Call between utterances."""
        self._state = np.zeros(_STATE_SHAPE, dtype=np.float32)
        self._pending = np.zeros(0, dtype=np.float32)
        self._context = np.zeros(CONTEXT_SAMPLES, dtype=np.float32)
        self._speaking = False
        self._above = 0
        self._below = 0
        self._last_probability = 0.0
