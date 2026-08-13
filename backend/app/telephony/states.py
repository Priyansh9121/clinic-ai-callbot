"""Call lifecycle state machine — the third and outermost state domain.

There are now three, and conflating any two of them is how a telephony bug
becomes a clinic bug:

* ``ConversationState`` (app/conversation/states.py) — the *clinic* workflow.
  Identifying the caller, choosing a slot, confirming a booking. It does not
  know audio exists, and is identical over text, local voice and the phone.
* ``VoiceState`` (app/voice/states.py) — the *audio* workflow. Listening,
  transcribing, speaking, interrupted. It does not know appointments exist,
  and is identical over a microphone and a phone line.
* ``CallState`` here — the *telephone call* itself. Ringing, answered,
  transferring, hung up. It does not know either of the other two exist.

They advance independently and legitimately disagree. A caller can be in
``CONFIRM_BOOKING`` (conversation) while ``SPEAKING`` (voice) on a call that is
``ACTIVE`` (call). Only the outermost one ends when the caller hangs up; the
other two are then torn down by the call layer, never the reverse.

The direction of control is one-way by design: telephony reacts to what the
conversation decided (``requires_human`` → transfer, ``ended`` → hang up). The
conversation layer never learns that SIP exists.
"""

from __future__ import annotations

from enum import StrEnum


class CallState(StrEnum):
    #: Inbound call signalled, not yet answered.
    RINGING = "RINGING"
    #: Answering, and building the media path.
    ANSWERING = "ANSWERING"
    #: Answered and media is flowing; the voice session is starting.
    CONNECTED = "CONNECTED"
    #: Voice session running. The steady state of a call.
    ACTIVE = "ACTIVE"
    #: Handing the caller to an approved destination.
    TRANSFERRING = "TRANSFERRING"
    #: Winding down: final audio draining before the channel is released.
    HANGING_UP = "HANGING_UP"
    #: Finished normally.
    ENDED = "ENDED"
    #: Finished abnormally — media loss, PBX failure, or a rejected setup.
    FAILED = "FAILED"


#: A call in one of these is finished; no further transition is legal and the
#: session resources should already have been released.
TERMINAL_STATES: frozenset[CallState] = frozenset({CallState.ENDED, CallState.FAILED})

#: States in which the caller is connected and audio is expected to flow.
#: Used to decide whether a media stall is a fault or just an idle channel.
MEDIA_STATES: frozenset[CallState] = frozenset(
    {CallState.CONNECTED, CallState.ACTIVE, CallState.TRANSFERRING}
)

#: Legal transitions. Enforced rather than documented, because the failure mode
#: of an unenforced call state machine is a call that is "active" after the
#: caller has hung up, holding a database session and a Whisper worker.
_ALLOWED: dict[CallState, frozenset[CallState]] = {
    CallState.RINGING: frozenset({CallState.ANSWERING, CallState.ENDED, CallState.FAILED}),
    CallState.ANSWERING: frozenset({CallState.CONNECTED, CallState.ENDED, CallState.FAILED}),
    CallState.CONNECTED: frozenset(
        {CallState.ACTIVE, CallState.HANGING_UP, CallState.ENDED, CallState.FAILED}
    ),
    CallState.ACTIVE: frozenset(
        {
            CallState.TRANSFERRING,
            CallState.HANGING_UP,
            CallState.ENDED,
            CallState.FAILED,
        }
    ),
    # A failed transfer returns the caller to us rather than dropping them —
    # see docs/architecture/telephony.md.
    CallState.TRANSFERRING: frozenset(
        {CallState.ACTIVE, CallState.HANGING_UP, CallState.ENDED, CallState.FAILED}
    ),
    CallState.HANGING_UP: frozenset({CallState.ENDED, CallState.FAILED}),
    CallState.ENDED: frozenset(),
    CallState.FAILED: frozenset(),
}


class IllegalCallTransition(RuntimeError):
    """Raised when a call is moved to a state it cannot reach from here."""

    def __init__(self, current: CallState, requested: CallState) -> None:
        super().__init__(f"cannot move a call from {current} to {requested}")
        self.current = current
        self.requested = requested


def can_transition(current: CallState, requested: CallState) -> bool:
    return requested in _ALLOWED[current]


def next_state(current: CallState, requested: CallState) -> CallState:
    """Validate a transition, raising rather than silently accepting it."""
    if not can_transition(current, requested):
        raise IllegalCallTransition(current, requested)
    return requested
