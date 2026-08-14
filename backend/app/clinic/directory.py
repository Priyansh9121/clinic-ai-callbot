"""Answers about who works here and what can be booked.

These two questions look like the other clinic FAQs and are not. "What are your
opening hours?" is a sentence a clinic signs off once; "Who are your doctors?"
changes when someone is hired, leaves or stops taking new patients, and the
answer has to match what the booking flow will actually offer thirty seconds
later. A knowledge file cannot promise that. The database can, and it is
already the source of truth for both — ``practitioners`` and
``appointment_types``, both carrying an ``active`` flag.

So these answers are composed from live rows rather than looked up. The model
is not involved: it classifies the question, and the sentence is built here
from what the tables say.

Both listings filter to active records, which is the property that matters. A
practitioner who has left must not be offered, and offering one is worse than
declining to answer — the caller asks for them by name and the booking flow
then cannot find them.
"""

from __future__ import annotations

import re

from app.database.models.appointment_type import AppointmentType
from app.database.models.practitioner import Practitioner

#: Questions about the people. Whole-word matching, like the knowledge lookup.
_PRACTITIONER_PATTERNS = (
    re.compile(r"\b(who|which|what)\b.{0,30}\b(doctors?|gps?|practitioners?|clinicians?)\b"),
    re.compile(r"\b(doctors?|gps?|practitioners?)\b.{0,20}\b(do you have|work|available|there)\b"),
    re.compile(r"\bwho\b.{0,20}\b(can i (see|book)|is available|works)\b"),
    re.compile(r"\b(list|tell me).{0,20}\b(doctors?|practitioners?)\b"),
    # "do you have Dr Patel", "is Dr Patel there", "can I see Dr Patel"
    re.compile(r"\b(do you have|is|are|can i see|can i book with)\b.{0,20}\bdr\b"),
    re.compile(r"\bdoes\b.{0,20}\bdr\b.{0,25}\b(work|still|there)\b"),
)

#: Questions about what can be booked.
_APPOINTMENT_TYPE_PATTERNS = (
    re.compile(r"\b(what|which)\b.{0,30}\b(appointments?|appointment types?|consultations?)\b"),
    re.compile(r"\bwhat\b.{0,20}\bcan i book\b"),
    re.compile(r"\b(kinds?|types?|sorts?)\b.{0,20}\b(of )?(appointments?|consultations?)\b"),
)


def asks_about_practitioners(question: str) -> bool:
    return any(p.search(question) for p in _PRACTITIONER_PATTERNS)


def asks_about_appointment_types(question: str) -> bool:
    return any(p.search(question) for p in _APPOINTMENT_TYPE_PATTERNS)


def join_naturally(items: list[str]) -> str:
    """ "a, b and c" — read aloud, so no Oxford comma and no bullet list."""
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    return f"{', '.join(items[:-1])} and {items[-1]}"


def describe_practitioners(practitioners: list[Practitioner]) -> str:
    """Name the clinicians a caller could ask for.

    Only the display name is spoken. Availability is deliberately not promised
    here — that is the booking search's answer, and stating it twice invites
    the two to disagree.
    """
    if not practitioners:
        # Not an error: a clinic mid-setup has no practitioners yet, and the
        # caller should be handed to a person rather than told "none".
        return ""
    names = [p.display_name for p in practitioners]
    if len(names) == 1:
        return f"{names[0]} is our practitioner here."
    return f"Our practitioners are {join_naturally(names)}."


def describe_appointment_types(types: list[AppointmentType]) -> str:
    """List what can actually be booked, by spoken name.

    The slug is an internal key and is never read out; ``name`` is the field a
    clinic writes for humans.
    """
    if not types:
        return ""
    names = [t.name for t in types]
    if len(names) == 1:
        return f"We offer {names[0]} appointments."
    return f"We offer {join_naturally(names)}."


def find_practitioner_by_name(
    practitioners: list[Practitioner], question: str
) -> Practitioner | None:
    """Resolve "do you have Dr Patel?" against live rows.

    Surname match on a whole word, because that is what callers say. Returns
    None rather than a guess: naming the wrong clinician is worse than asking
    the caller to repeat themselves.
    """
    lowered = question.lower()
    for practitioner in practitioners:
        surname = practitioner.last_name.lower()
        if surname and re.search(rf"\b{re.escape(surname)}\b", lowered):
            return practitioner
    return None
