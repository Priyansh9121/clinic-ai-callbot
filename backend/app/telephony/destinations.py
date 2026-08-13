"""Where a call may be sent, and nowhere else.

The single most dangerous thing a telephony layer can do is turn caller-derived
text into a dial string. A transcript is untrusted input, a model completion is
untrusted output, and either becoming an Asterisk destination is toll fraud with
extra steps — "transfer me to 0011 44…" or "dial 1900…".

So there is no code path from text to a destination. The only values that can
be dialled are the members of ``TransferDestination``, and each one resolves to
an extension that came from *configuration*. Resolution is a dictionary lookup
on an enum: an unknown key cannot produce a dial string, it raises.

``TransferReason`` (the clinic's reason for escalating) is deliberately a
separate type from ``TransferDestination`` (where the call physically goes).
Mapping between them is a closed, deterministic table here — not a decision the
conversation layer or a model gets to make.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

from app.conversation.states import TransferReason


class TransferDestination(StrEnum):
    """Approved call destinations. This enum is the entire allowlist."""

    #: A human at the front desk. The normal escalation.
    RECEPTION = "RECEPTION"
    #: Recorded message for out-of-hours or unattended reception.
    VOICEMAIL = "VOICEMAIL"


#: Every clinic escalation reason maps to a destination here, deterministically.
#: SAFETY maps to RECEPTION, *not* to emergency services: the clinic system
#: advises the caller to ring 000 themselves and the wording is owned by the
#: SafetyEngine. Automatic emergency dialling is out of scope and would need
#: legal review — see docs/architecture/telephony.md.
_REASON_TO_DESTINATION: dict[TransferReason, TransferDestination] = {
    TransferReason.CALLER_REQUEST: TransferDestination.RECEPTION,
    TransferReason.SAFETY: TransferDestination.RECEPTION,
    TransferReason.CLINICAL_QUESTION: TransferDestination.RECEPTION,
    TransferReason.REPEATED_MISUNDERSTANDING: TransferDestination.RECEPTION,
    TransferReason.VERIFICATION_FAILED: TransferDestination.RECEPTION,
    TransferReason.UNSUPPORTED_REQUEST: TransferDestination.RECEPTION,
    TransferReason.NO_AVAILABILITY: TransferDestination.RECEPTION,
    TransferReason.TECHNICAL_FAILURE: TransferDestination.RECEPTION,
    TransferReason.KNOWLEDGE_GAP: TransferDestination.RECEPTION,
}

#: Extensions are dialplan-local: digits, or a short alphanumeric context name.
#: Anything containing a SIP URI, an outside line prefix, a comma (Asterisk
#: argument separator) or whitespace is rejected at configuration load, so a
#: misconfigured .env cannot become an outbound dial string either.
_SAFE_EXTENSION = re.compile(r"^[A-Za-z0-9_-]{1,20}$")


class UnknownDestination(ValueError):
    """Raised when something that is not an approved destination is dialled."""


class UnsafeExtension(ValueError):
    """Raised when configuration supplies an extension we refuse to dial."""


@dataclass(frozen=True, slots=True)
class TransferTarget:
    """A resolved, dialable destination."""

    destination: TransferDestination
    extension: str
    #: Asterisk context. Restricting transfers to a dedicated context is what
    #: stops a transfer reaching an outbound trunk — see the dialplan template.
    context: str

    @property
    def endpoint(self) -> str:
        """Dial target as Asterisk expects it, for logging and ARI calls."""
        return f"{self.context},{self.extension},1"


class DestinationRegistry:
    """Resolves approved destinations to configured extensions.

    Built once from settings. Holds no caller state, so it is safe to share
    across concurrent calls.
    """

    def __init__(self, extensions: dict[TransferDestination, str], *, context: str) -> None:
        if not _SAFE_EXTENSION.match(context):
            raise UnsafeExtension(f"transfer context is not a bare dialplan context: {context!r}")
        for destination, extension in extensions.items():
            if not _SAFE_EXTENSION.match(extension):
                raise UnsafeExtension(
                    f"extension for {destination} is not a bare extension: {extension!r}"
                )
        self._extensions = dict(extensions)
        self._context = context

    @property
    def context(self) -> str:
        return self._context

    def configured(self) -> frozenset[TransferDestination]:
        return frozenset(self._extensions)

    def resolve(self, destination: TransferDestination) -> TransferTarget:
        """Resolve an approved destination, raising for anything else.

        Takes a ``TransferDestination`` — not a string — so a caller-supplied
        value cannot reach this function without first failing enum coercion.
        """
        if not isinstance(destination, TransferDestination):
            raise UnknownDestination(f"not an approved destination: {destination!r}")
        extension = self._extensions.get(destination)
        if extension is None:
            raise UnknownDestination(f"{destination} is not configured for this deployment")
        return TransferTarget(destination=destination, extension=extension, context=self._context)

    def for_reason(self, reason: TransferReason | None) -> TransferTarget:
        """Map a clinic escalation reason to a destination, then resolve it.

        An unrecognised or missing reason falls back to reception rather than
        failing: a caller who needs a human should reach one even if the reason
        code is something this table has not seen.
        """
        destination = _REASON_TO_DESTINATION.get(reason, TransferDestination.RECEPTION)
        try:
            return self.resolve(destination)
        except UnknownDestination:
            if destination is not TransferDestination.RECEPTION:
                return self.resolve(TransferDestination.RECEPTION)
            raise
