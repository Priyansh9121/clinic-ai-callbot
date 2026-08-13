"""Call control — answer, hang up, transfer. Signalling only, never media.

Call control and audio transport are different problems with different failure
modes, so they are different objects. Control is request/response and rare
(a handful of operations per call); media is a continuous 50 Hz stream. Fusing
them produces a class where a blocked REST call stalls audio, which is exactly
the bug that makes a phone agent sound broken.

The clinic layer depends on this Protocol, not on Asterisk. ``FakeCallController``
in the tests implements it, and the entire call lifecycle is testable with no
PBX present.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, runtime_checkable

from app.telephony.destinations import TransferTarget
from app.telephony.metadata import CallerIdentity, CallMetadata


class HangupCause(StrEnum):
    """Why a call ended. Operational, not clinical."""

    #: The caller hung up.
    CALLER = "CALLER"
    #: The clinic application ended the call after a completed conversation.
    APPLICATION = "APPLICATION"
    #: Handed to a human; the AI leg is finished.
    TRANSFERRED = "TRANSFERRED"
    #: Capacity, PBX failure, or media loss.
    FAILURE = "FAILURE"


class TransferOutcome(StrEnum):
    COMPLETED = "COMPLETED"
    #: Destination rang out, was busy, or rejected the call. The caller is
    #: still with us and must be told something.
    UNAVAILABLE = "UNAVAILABLE"
    #: The PBX refused or the control channel failed.
    FAILED = "FAILED"


@dataclass(frozen=True, slots=True)
class TransferResult:
    outcome: TransferOutcome
    target: TransferTarget | None = None
    #: Short machine-readable detail for logs. Never caller content.
    detail: str | None = None

    @property
    def succeeded(self) -> bool:
        return self.outcome is TransferOutcome.COMPLETED


@dataclass(frozen=True, slots=True)
class DtmfEvent:
    """A keypad press, delivered as structured input.

    DTMF is *not* audio and must never be routed through STT: the tones are
    in-band but the PBX has already decoded them, and re-recognising them as
    speech would turn "1" into "one", "won" or nothing at all.
    """

    digit: str
    call_id: str


@runtime_checkable
class CallController(Protocol):
    """Signalling operations for one call."""

    @property
    def call_id(self) -> str: ...

    @property
    def caller(self) -> CallerIdentity: ...

    @property
    def metadata(self) -> CallMetadata: ...

    async def answer(self) -> None:
        """Answer the inbound call. Idempotent."""
        ...

    async def hangup(self, cause: HangupCause = HangupCause.APPLICATION) -> None:
        """Release the channel. Idempotent, and safe after the caller has gone."""
        ...

    async def transfer(self, target: TransferTarget) -> TransferResult:
        """Send the caller to an approved destination.

        Takes a resolved ``TransferTarget`` rather than a string, so there is
        no signature through which caller text could become a dial string.
        """
        ...

    def on_hangup(self, callback: Callable[[HangupCause], None]) -> None:
        """Register a callback for the caller hanging up.

        Fired from the PBX event stream, so implementations must keep it
        non-blocking — the callback schedules teardown, it does not perform it.
        """
        ...

    def on_dtmf(self, callback: Callable[[DtmfEvent], None]) -> None:
        """Register a callback for keypad input."""
        ...
