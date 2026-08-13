"""Voice mechanics state machine, and the turn token that guards against
stale responses.

**This is not ``ConversationState``.** They model different things and must not
be conflated:

* ``ConversationState`` (app/conversation/states.py) is the *clinic* workflow —
  identifying the caller, choosing a slot, confirming a booking. It is unaware
  that audio exists and is identical over text and voice.
* ``VoiceState`` here is the *audio* workflow — listening, transcribing,
  speaking, interrupted. It is unaware of appointments.

A caller can be in ``CONFIRM_BOOKING`` (conversation) while in ``SPEAKING``
(voice); the two advance independently.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from enum import StrEnum


class VoiceState(StrEnum):
    #: Session created, not yet capturing.
    IDLE = "IDLE"
    #: Capturing, waiting for the caller to start.
    LISTENING = "LISTENING"
    #: VAD has speech; accumulating the utterance.
    SPEECH_DETECTED = "SPEECH_DETECTED"
    #: Endpointed; running STT.
    TRANSCRIBING = "TRANSCRIBING"
    #: Transcript handed to the ConversationEngine.
    THINKING = "THINKING"
    #: Playing the agent's reply.
    SPEAKING = "SPEAKING"
    #: Caller spoke over the agent; playback is being torn down.
    INTERRUPTED = "INTERRUPTED"
    #: Shutting down.
    STOPPING = "STOPPING"


#: States in which caller speech constitutes a barge-in rather than a new turn.
#: The agent is producing audio in all of them, including THINKING because an
#: acknowledgement ("One moment while I check.") may still be playing.
INTERRUPTIBLE_STATES: frozenset[VoiceState] = frozenset({VoiceState.SPEAKING, VoiceState.THINKING})


class StaleTurnError(RuntimeError):
    """Raised when work completes for a turn that is no longer current."""


@dataclass
class TurnToken:
    """Identity and cancellation state for one caller turn.

    Every piece of work in a turn — STT, the engine call, synthesis, playback —
    carries the token it was started for. Before anything reaches the speaker,
    ``ensure_active`` is checked. That is the mechanism that stops a slow turn
    from talking over the turn that replaced it.

    Cancellation is one-way. A cancelled token never becomes valid again; the
    next turn gets a fresh one.
    """

    id: int
    _cancelled: bool = field(default=False, init=False)

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    def cancel(self) -> None:
        self._cancelled = True

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        return f"TurnToken(id={self.id}, cancelled={self._cancelled})"


class TurnTracker:
    """Issues turn tokens and knows which one is current.

    Per-session, never global: two concurrent callers must not share turn
    numbering or be able to cancel each other.
    """

    def __init__(self) -> None:
        self._counter = itertools.count(1)
        self._current: TurnToken | None = None

    @property
    def current(self) -> TurnToken | None:
        return self._current

    def begin(self) -> TurnToken:
        """Start a new turn, cancelling any turn still in flight."""
        if self._current is not None and not self._current.cancelled:
            self._current.cancel()
        self._current = TurnToken(id=next(self._counter))
        return self._current

    def is_active(self, token: TurnToken) -> bool:
        return not token.cancelled and self._current is not None and self._current.id == token.id

    def ensure_active(self, token: TurnToken) -> None:
        """Raise if this turn has been superseded or cancelled.

        Called immediately before any audio is queued for playback.
        """
        if not self.is_active(token):
            raise StaleTurnError(f"turn {token.id} is no longer active")

    def cancel_current(self) -> None:
        if self._current is not None:
            self._current.cancel()
