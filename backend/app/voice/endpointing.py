"""Turn endpointing — deciding when the caller has finished speaking.

The VAD answers "is this 20 ms frame speech?". Endpointing answers the question
that actually matters: "has this person finished their turn?". They are
different problems and are deliberately separate classes.

Waiting for a flat 3 s of silence would work and feel awful. What this does
instead is track the shape of the utterance and apply distinct thresholds:

* ``end_silence_ms`` — silence after real speech ends the turn. This is the
  number that decides whether the agent feels attentive or sluggish; it is
  benchmarked in ``scripts/benchmark_voice.py``, not guessed.
* ``min_speech_ms`` — utterances shorter than this are noise (a cough, a door),
  not a turn. Without it, every clatter produces an empty transcript and a
  "Sorry, I didn't catch that".
* ``initial_silence_ms`` — the caller has said nothing at all yet. Longer than
  ``end_silence_ms`` because someone gathering their thoughts before speaking
  is normal, whereas a pause *mid-answer* is usually the end of it.
* ``max_utterance_ms`` — a hard cap. Somebody who never stops, a stuck line, or
  a television in the background must not buffer minutes of audio.

All thresholds are configuration, not constants.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import StrEnum

from app.speech.providers import FRAME_MS, SpeechEvent


class EndpointReason(StrEnum):
    """Why the turn ended. Drives both metrics and the agent's response."""

    #: Silence after speech — the normal case.
    END_SILENCE = "END_SILENCE"
    #: Hard duration cap reached while still speaking.
    MAX_DURATION = "MAX_DURATION"
    #: Nothing said at all within the initial window.
    NO_SPEECH = "NO_SPEECH"
    #: Speech was too brief to be a real utterance.
    TOO_SHORT = "TOO_SHORT"


@dataclass(frozen=True, slots=True)
class EndpointConfig:
    end_silence_ms: int = 500
    initial_silence_ms: int = 6_000
    min_speech_ms: int = 200
    max_utterance_ms: int = 20_000

    def __post_init__(self) -> None:
        if self.end_silence_ms <= 0 or self.min_speech_ms < 0:
            raise ValueError("endpoint thresholds must be positive")


@dataclass(frozen=True, slots=True)
class Endpoint:
    """The decision that a turn has ended."""

    reason: EndpointReason
    speech_ms: float
    #: Milliseconds from the last speech frame to the endpoint decision. This
    #: is the latency the caller actually perceives before the agent replies.
    endpoint_latency_ms: float

    @property
    def is_usable(self) -> bool:
        """Whether this should be transcribed and sent to the engine."""
        return self.reason in (EndpointReason.END_SILENCE, EndpointReason.MAX_DURATION)


@dataclass
class Endpointer:
    """Consumes VAD events frame by frame and decides when the turn is over.

    Time is measured in frames rather than a wall clock so that the same logic
    is exact and reproducible in tests, and unaffected by scheduling jitter in
    production. ``monotonic`` is used only to report the real latency.
    """

    config: EndpointConfig = field(default_factory=EndpointConfig)

    _speech_frames: int = field(default=0, init=False)
    _silence_frames: int = field(default=0, init=False)
    _total_frames: int = field(default=0, init=False)
    _speech_started: bool = field(default=False, init=False)
    _finished: bool = field(default=False, init=False)
    _last_speech_at: float | None = field(default=None, init=False)
    _started_at: float | None = field(default=None, init=False)

    @property
    def speech_started(self) -> bool:
        return self._speech_started

    @property
    def speech_ms(self) -> float:
        return self._speech_frames * FRAME_MS

    @property
    def silence_ms(self) -> float:
        return self._silence_frames * FRAME_MS

    @property
    def utterance_ms(self) -> float:
        return self._total_frames * FRAME_MS

    def process(self, event: SpeechEvent | None, *, is_speech: bool) -> Endpoint | None:
        """Advance by one frame. Returns an ``Endpoint`` when the turn is over.

        ``event`` is the VAD's transition (or None); ``is_speech`` is its
        instantaneous state. Using both means a long utterance with internal
        pauses is measured correctly rather than being cut at the first gap.
        """
        if self._finished:
            return None

        now = time.monotonic()
        if self._started_at is None:
            self._started_at = now
        self._total_frames += 1

        if event is SpeechEvent.SPEECH_STARTED:
            self._speech_started = True

        if is_speech:
            self._speech_frames += 1
            self._silence_frames = 0
            self._last_speech_at = now
        else:
            self._silence_frames += 1

        # Hard cap first: it must win even mid-speech.
        if self.utterance_ms >= self.config.max_utterance_ms:
            return self._finish(EndpointReason.MAX_DURATION, now)

        if not self._speech_started:
            if self.utterance_ms >= self.config.initial_silence_ms:
                return self._finish(EndpointReason.NO_SPEECH, now)
            return None

        if self.silence_ms >= self.config.end_silence_ms:
            reason = (
                EndpointReason.END_SILENCE
                if self.speech_ms >= self.config.min_speech_ms
                else EndpointReason.TOO_SHORT
            )
            return self._finish(reason, now)
        return None

    def _finish(self, reason: EndpointReason, now: float) -> Endpoint:
        self._finished = True
        # Latency is measured from the last *speech* frame, not from the
        # endpoint decision, because that is the silence the caller sits in.
        latency = (now - self._last_speech_at) * 1000 if self._last_speech_at else 0.0
        return Endpoint(
            reason=reason,
            speech_ms=self.speech_ms,
            endpoint_latency_ms=round(latency, 1),
        )

    def reset(self) -> None:
        self._speech_frames = 0
        self._silence_frames = 0
        self._total_frames = 0
        self._speech_started = False
        self._finished = False
        self._last_speech_at = None
        self._started_at = None
