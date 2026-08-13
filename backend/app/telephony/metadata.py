"""Caller metadata — normalised, minimised, and never authentication.

Caller ID arrives with every inbound call and is tempting to trust. It must not
be trusted:

* it is **absent** on withheld and many VoIP calls;
* it is **shared** — a family, a share house, a workplace switchboard;
* it is **forwarded** — the number presented may be the diverting party's;
* it is **trivially spoofed**, which is the basis of most vishing.

So this module produces metadata for operations (did the call connect, was it
withheld) and nothing that can raise a caller's privilege. The existing
surname + date-of-birth verification remains the only way to become
``IDENTIFIED``, and Milestone 5's OTP the only way to become ``VERIFIED``.

The number is also *not* given to the language model. It is not in a prompt, not
in conversation memory, and not in a log line — only a salted fingerprint is
kept, which supports "is this the same caller redialling?" without storing who
they are.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

#: Asterisk presents these for withheld or unavailable caller ID, in various
#: capitalisations depending on the trunk.
_ANONYMOUS_MARKERS = frozenset(
    {"", "anonymous", "unknown", "private", "restricted", "unavailable", "withheld"}
)

_NON_DIAL = re.compile(r"[^\d+]")


def normalise_number(raw: str | None) -> str | None:
    """Reduce a presented number to digits, or ``None`` if it is not one.

    Australian-aware: a national ``04xx``/``0x`` number is converted to E.164
    so that the same caller fingerprints identically whether the trunk presents
    national or international format.
    """
    if raw is None:
        return None
    candidate = raw.strip()
    if candidate.lower() in _ANONYMOUS_MARKERS:
        return None
    candidate = _NON_DIAL.sub("", candidate)
    if not candidate or not any(c.isdigit() for c in candidate):
        return None
    if candidate.startswith("+"):
        return candidate
    if candidate.startswith("0061"):
        return "+61" + candidate[4:]
    if candidate.startswith("61") and len(candidate) >= 11:
        return "+" + candidate
    if candidate.startswith("0") and len(candidate) == 10:
        return "+61" + candidate[1:]
    return candidate


@dataclass(frozen=True, slots=True)
class CallerIdentity:
    """What we know about who is calling. Deliberately very little.

    ``number`` is held in memory for the life of the call so that a transfer
    can present it to reception, and is never persisted, logged or prompted.
    """

    number: str | None = None
    name: str | None = None
    anonymous: bool = True

    @classmethod
    def from_channel(cls, *, number: str | None, name: str | None = None) -> CallerIdentity:
        normalised = normalise_number(number)
        presented = (name or "").strip()
        if presented.lower() in _ANONYMOUS_MARKERS:
            presented = ""
        return cls(
            number=normalised,
            name=presented or None,
            anonymous=normalised is None,
        )

    def fingerprint(self, *, salt: str) -> str | None:
        """Stable pseudonym for a number, for metrics and repeat detection.

        Salted so the digest cannot be reversed by hashing every Australian
        mobile number — an unsalted hash of a 10-digit number is not an
        anonymisation, it is a lookup table.
        """
        if self.number is None:
            return None
        digest = hashlib.sha256(f"{salt}:{self.number}".encode())
        return digest.hexdigest()[:16]

    def log_fields(self) -> dict[str, object]:
        """Safe to log: presence and shape, never the value."""
        return {
            "caller_present": not self.anonymous,
            "caller_name_present": self.name is not None,
        }

    def __repr__(self) -> str:  # pragma: no cover - defensive against tracebacks
        # A traceback or a debugger session must not print a caller's number.
        return f"CallerIdentity(anonymous={self.anonymous}, number=[redacted])"


@dataclass(frozen=True, slots=True)
class CallMetadata:
    """Non-identifying facts about a call, safe for logs and metrics."""

    call_id: str
    #: The dialplan extension the caller reached, e.g. the AI extension. Not
    #: the caller's own number.
    dialed_extension: str | None = None
    #: Opaque channel name from the PBX, for correlating with Asterisk logs.
    channel: str | None = None

    def log_fields(self) -> dict[str, object]:
        return {"call_id": self.call_id, "extension": self.dialed_extension}
