"""Deterministic baseline NLU.

This is intentionally simple: keyword and pattern matching with explicit
priorities. Its purpose is twofold.

1. The system works end-to-end with no model at all, so a failed or slow LLM
   degrades to something that still books appointments rather than to nothing.
2. It is the measured baseline. Milestone 2 adds an LLU-backed analyser behind
   the same ``MessageAnalyser`` interface, and its accuracy is compared against
   these numbers on the same conversation fixtures.

Interface contract for any future implementation: return an ``Analysis``.
Extracting an entity means resolving it to a **database id** from the supplied
vocabulary — never to free text that a downstream component would have to
interpret.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from datetime import date, time
from typing import Protocol, runtime_checkable

from app.conversation.nlu.dates import (
    MONTHS,
    ORDINAL_WORDS,
    DateResolution,
    TimeResolution,
    parse_clock_time,
    resolve_date,
    resolve_time,
)
from app.conversation.states import Intent, PatientType
from app.core.timeutils import DayPeriod
from app.safety.engine import normalise

# Keyword matching uses the aggressive normalisation shared with the safety
# engine, which strips all punctuation. Date and time parsing cannot: "10:30"
# and "21/08" lose their meaning without the separator. So entity extraction
# runs over a softer form that keeps `: / - .` while still lowercasing,
# removing accents and collapsing whitespace.
_SOFT_STRIP = re.compile(r"[^a-z0-9\s:/.\-]+")
_SOFT_WS = re.compile(r"\s+")


def soft_normalise(text: str) -> str:
    import unicodedata

    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = text.lower().replace("'", "").replace("\u2019", "")
    text = _SOFT_STRIP.sub(" ", text)
    return _SOFT_WS.sub(" ", text).strip()


# --- Vocabulary -------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class PractitionerTerm:
    practitioner_id: uuid.UUID
    display_name: str
    #: Lowercase surface forms: surname, "dr patel", full name.
    aliases: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class AppointmentTypeTerm:
    appointment_type_id: uuid.UUID
    slug: str
    name: str
    aliases: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Vocabulary:
    """Clinic-specific terms, loaded from the database once per turn.

    Matching against real records is what prevents a hallucinated practitioner:
    if the caller names someone who is not here, no id is produced and the
    engine asks again rather than inventing a booking.
    """

    practitioners: tuple[PractitionerTerm, ...] = ()
    appointment_types: tuple[AppointmentTypeTerm, ...] = ()

    def find_practitioner(self, text: str) -> PractitionerTerm | None:
        best: tuple[int, PractitionerTerm] | None = None
        for term in self.practitioners:
            for alias in term.aliases:
                # Longest alias wins: "dr sarah patel" beats "patel".
                if (best is None or len(alias) > best[0]) and re.search(
                    rf"\b{re.escape(alias)}\b", text
                ):
                    best = (len(alias), term)
        return best[1] if best else None

    def find_appointment_type(self, text: str) -> AppointmentTypeTerm | None:
        best: tuple[int, AppointmentTypeTerm] | None = None
        for term in self.appointment_types:
            for alias in term.aliases:
                if (best is None or len(alias) > best[0]) and re.search(
                    rf"\b{re.escape(alias)}\b", text
                ):
                    best = (len(alias), term)
        return best[1] if best else None


# --- Analysis result --------------------------------------------------------
@dataclass(slots=True)
class Entities:
    practitioner_id: uuid.UUID | None = None
    practitioner_name: str | None = None
    practitioner_any: bool = False
    appointment_type_id: uuid.UUID | None = None
    appointment_type_name: str | None = None
    date_resolution: DateResolution = field(default_factory=DateResolution)
    time_resolution: TimeResolution = field(default_factory=TimeResolution)
    patient_type: PatientType | None = None
    #: 1-based position from "the first one" / "number two".
    selection_ordinal: int | None = None
    #: From "the 10:30 one" — matched against offered slots by the flow.
    selection_time: time | None = None
    last_name: str | None = None
    first_name: str | None = None
    date_of_birth: date | None = None
    mobile: str | None = None

    @property
    def preferred_date(self) -> date | None:
        return self.date_resolution.day

    @property
    def preferred_period(self) -> DayPeriod:
        return self.time_resolution.period

    @property
    def not_before(self) -> time | None:
        return self.time_resolution.not_before

    def has_any(self) -> bool:
        return any(
            (
                self.practitioner_id,
                self.practitioner_any,
                self.appointment_type_id,
                not self.date_resolution.is_empty,
                not self.time_resolution.is_empty,
                self.patient_type,
                self.selection_ordinal,
                self.selection_time,
            )
        )


@dataclass(slots=True)
class Analysis:
    intent: Intent = Intent.UNKNOWN
    confidence: float = 0.0
    entities: Entities = field(default_factory=Entities)
    affirmative: bool = False
    negative: bool = False
    #: "actually", "no wait", "I meant" — the caller is revising something.
    is_correction: bool = False
    #: True when the utterance carried nothing actionable at all.
    is_empty: bool = False
    #: Phrased as a question. Used to prefer an approved clinic answer over the
    #: "they probably want to book" heuristic: "are you taking new patients?"
    #: is a question about the clinic, not a statement that they are one.
    is_question: bool = False
    normalised: str = ""


@runtime_checkable
class MessageAnalyser(Protocol):
    async def analyse(
        self, text: str, *, vocabulary: Vocabulary, today: date, state_hint: str | None = None
    ) -> Analysis: ...


# --- Patterns ---------------------------------------------------------------
# Ordered by priority: the first matching intent wins. Human transfer sits
# above the task intents so "I'd rather just book with a person" transfers.
_INTENT_PATTERNS: tuple[tuple[Intent, str, float], ...] = (
    (
        Intent.HUMAN_RECEPTION,
        r"\b(speak|talk|chat) (to|with) (a |an |someone|somebody)?"
        r"(human|person|receptionist|reception|staff|real person|nurse|doctor)\b"
        r"|\b(put me through|transfer me|get me) (to )?(reception|someone|a person|a human)\b"
        r"|\b(i want|id like|i would like|can i get|let me) (to )?(speak|talk) (to|with)\b"
        # "I want reception" / "give me a person" — no verb of speaking.
        r"|\b(i want|id like|i need|give me|can i have|just get me)\s+(to\s+)?"
        r"(reception|the receptionist|a human|a person|someone|an operator)\b"
        # A request for a person must be recognised deterministically, never
        # left to a model: the front-desk phrasings and explicit rejections of
        # automation below are common and must not depend on the LLM being up.
        r"|\b(front desk|frontdesk)\b"
        r"|\b(dont|do not|rather not|dont want to) (want to )?(talk|speak|deal) "
        r"(to|with)? ?(a )?(robot|machine|computer|bot|automated)\b"
        r"|\b(automated|automatic) (system|service|thing)\b"
        r"|\breal person\b|\bhuman being\b|\boperator\b",
        0.95,
    ),
    (
        Intent.RESCHEDULE_APPOINTMENT,
        r"\b(reschedul\w*|re-?schedule|move (my|the|an|that) appointment"
        r"|change (my|the) appointment"
        r"|change (the )?(time|day|date)|push (it|my appointment) back"
        r"|bring (it|my appointment) forward"
        r"|different (time|day)|another (time|day)"
        r"|cant make (it|friday|monday|tuesday|wednesday"
        r"|thursday|saturday|sunday|my appointment)|swap my appointment)\b",
        0.9,
    ),
    (
        Intent.CANCEL_APPOINTMENT,
        r"\b(cancel|cancell\w+)\b(?!.*\bpolicy\b)"
        r"|\b(dont|do not) need (my|the) appointment\b"
        r"|\bcall off my appointment\b",
        0.9,
    ),
    (
        Intent.BOOK_APPOINTMENT,
        r"\b(book|booking|make|schedule|arrange|set up|get|need|want|like)\b.{0,25}"
        r"\b(appointment|appt|booking|visit|consult\w*|check.?up|slot|time)\b"
        r"|\b(appointment|appt)\b.{0,20}\b(please|available|free|possible)\b"
        r"|\b(see|seeing) (a |the )?(doctor|gp|dr\b)"
        r"|\b(come in|get in) (to see|and see)\b"
        r"|\bany(thing| times?| appointments?)? (available|free|open)\b",
        0.85,
    ),
    (
        Intent.CLINIC_INFORMATION,
        r"\b(what time|when) (do|are) you (open|close|closing)\b"
        r"|\b(opening|closing) hours\b|\bhow much\b|\bwhat.{0,10}cost\b"
        r"|\bwhere are you\b|\bwhats your address\b|\bwhat is your address\b"
        r"|\b(do|can) you (do|offer|have|bulk bill)\b"
        r"|\bparking\b|\bbulk bill\w*\b|\bwheelchair\b|\btelehealth\b"
        r"|\b(cancellation|referral) polic\w+\b",
        0.8,
    ),
    (
        Intent.END_CALL,
        # Leading match rather than a whole-string match, so "no thanks,
        # that's all" is recognised as closing the call. Acting on this intent
        # is restricted by state in the engine, so a bare "no" mid-booking is
        # still just a negative answer.
        r"^(no thanks?|nothing else|thats (all|it)|that is (all|it)|im (good|done)"
        r"|all good|goodbye|bye|bye bye|nope)\b"
        r"|\b(nothing else|thats all|that is all|thats everything|hang up|end the call)\b"
        r"|^(thanks?|thank you|cheers)( very much)?$",
        0.85,
    ),
)

_AFFIRMATIVE = re.compile(
    r"^(yes|yeah|yep|yup|yes please|ok|okay|sure|correct|thats right|that is right|"
    r"right|confirm|confirmed|sounds good|perfect|great|please do|go ahead|do it|"
    r"absolutely|definitely|affirmative|thats correct|that is correct|of course)\b"
)
_NEGATIVE = re.compile(
    r"^(no|nope|nah|not really|negative|dont|do not|incorrect|thats wrong|that is wrong|"
    r"no thanks|not that one|neither|none of those|none of them)\b"
)
_QUESTION = re.compile(
    r"^(do|does|did|are|is|was|can|could|will|would|should|have|has|"
    r"what|whats|when|where|which|who|how|why|any)\b"
)
_CORRECTION = re.compile(
    r"\b(actually|instead|rather|i meant|i ment|sorry i|no wait|wait no|change that|"
    r"make that|scratch that|on second thought|can we make it|lets do)\b"
)
_ANY_PRACTITIONER = re.compile(
    r"\b(any(one| doctor| gp| practitioner)?( is| will be)? (fine|ok|okay|good|works)|"
    r"dont (mind|care)|do not (mind|care)|no preference|whoever|whichever|"
    r"first available|earliest|anyone available|not fussed|doesnt matter|does not matter)\b"
)
_NEW_PATIENT = re.compile(
    r"\b(new patient|im new|i am new|first time|never been|havent been before|"
    r"not been before|new here|never visited)\b"
)
_EXISTING_PATIENT = re.compile(
    r"\b(existing|existing patient|been before|im a patient|i am a patient|regular|"
    r"current patient|ive been|i have been|already a patient|yes ive been)\b"
)
_ORDINAL_SELECT = re.compile(
    r"\b(?:the\s+)?(first|1st|second|2nd|third|3rd|fourth|4th|last|earliest|earlier one)\b"
    r"|\bnumber (one|two|three|four|1|2|3|4)\b"
    r"|^([1-4])$"
)
_ORDINALS: dict[str, int] = {
    "first": 1,
    "1st": 1,
    "one": 1,
    "1": 1,
    "earliest": 1,
    "second": 2,
    "2nd": 2,
    "two": 2,
    "2": 2,
    "third": 3,
    "3rd": 3,
    "three": 3,
    "3": 3,
    "fourth": 4,
    "4th": 4,
    "four": 4,
    "4": 4,
}
#: States in which the caller is picking from a list that was just read out.
_CHOICE_STATES = frozenset(
    {
        "SELECT_SLOT",
        "CONFIRM_BOOKING",
        "RESCHEDULE_LOOKUP",
        "RESCHEDULE_SELECTION",
        "RESCHEDULE_CONFIRMATION",
        "CANCEL_LOOKUP",
        "CANCEL_CONFIRMATION",
    }
)
_DOB = re.compile(
    r"\b(\d{1,2})[/\-. ](\d{1,2})[/\-. ](\d{4})\b"
    r"|\b(\d{4})-(\d{1,2})-(\d{1,2})\b"
)
_MOBILE = re.compile(r"\b(?:\+?61|0)[45](?:[ -]?\d){8}\b")
_SURNAME_PHRASE = re.compile(r"\b(?:surname|last name|family name)\s+(?:is\s+)?([a-z][a-z'-]+)\b")
_FULLNAME_PHRASE = re.compile(
    r"\b(?:my name is|this is|it'?s|i'?m|name'?s)\s+([a-z][a-z'-]+)(?:\s+([a-z][a-z'-]+))?\b"
)


class BaselineAnalyser:
    """Deterministic ``MessageAnalyser``. No I/O, no model, no clock access."""

    def __init__(self) -> None:
        self._intent_patterns = [
            (intent, re.compile(pattern), confidence)
            for intent, pattern, confidence in _INTENT_PATTERNS
        ]

    async def analyse(
        self,
        text: str,
        *,
        vocabulary: Vocabulary,
        today: date,
        state_hint: str | None = None,
    ) -> Analysis:
        normalised = normalise(text)
        analysis = Analysis(normalised=normalised)
        if not normalised:
            analysis.is_empty = True
            return analysis

        analysis.affirmative = bool(_AFFIRMATIVE.search(normalised))
        analysis.negative = bool(_NEGATIVE.search(normalised))
        analysis.is_correction = bool(_CORRECTION.search(normalised))
        analysis.is_question = bool(_QUESTION.search(normalised))

        for intent, pattern, confidence in self._intent_patterns:
            if pattern.search(normalised):
                analysis.intent = intent
                analysis.confidence = confidence
                break

        analysis.entities = self._extract_entities(
            normalised,
            soft_normalise(text),
            vocabulary=vocabulary,
            today=today,
            state_hint=state_hint,
        )

        # An utterance carrying only entities ("Friday afternoon") is a valid
        # answer to a question, not a failure to understand.
        if analysis.intent is Intent.UNKNOWN and analysis.entities.has_any():
            analysis.confidence = 0.5
        analysis.is_empty = (
            analysis.intent is Intent.UNKNOWN
            and not analysis.entities.has_any()
            and not analysis.affirmative
            and not analysis.negative
        )
        return analysis

    def _extract_entities(
        self,
        normalised: str,
        soft: str,
        *,
        vocabulary: Vocabulary,
        today: date,
        state_hint: str | None,
    ) -> Entities:
        entities = Entities()

        # While the caller is giving their identity, a word like "Chen" is a
        # surname, not a practitioner. Restricting extraction by state avoids
        # a whole class of cross-talk bugs.
        identifying = state_hint in ("VERIFY_PATIENT", "NEW_PATIENT_DETAILS")

        if not identifying:
            if _ANY_PRACTITIONER.search(normalised):
                entities.practitioner_any = True
            practitioner = vocabulary.find_practitioner(normalised)
            if practitioner is not None:
                entities.practitioner_id = practitioner.practitioner_id
                entities.practitioner_name = practitioner.display_name
                entities.practitioner_any = False

            appointment_type = vocabulary.find_appointment_type(normalised)
            if appointment_type is not None:
                entities.appointment_type_id = appointment_type.appointment_type_id
                entities.appointment_type_name = appointment_type.name

            if _NEW_PATIENT.search(normalised):
                entities.patient_type = PatientType.NEW
            elif _EXISTING_PATIENT.search(normalised):
                entities.patient_type = PatientType.EXISTING

            entities.date_resolution = resolve_date(soft, today)
            entities.time_resolution = resolve_time(soft)

            match = _ORDINAL_SELECT.search(normalised)
            if match:
                choice = next((g for g in match.groups() if g), None)
                if choice == "last":
                    entities.selection_ordinal = -1
                elif choice:
                    entities.selection_ordinal = _ORDINALS.get(choice)
            entities.selection_time = parse_clock_time(soft)

            # While the caller is choosing between options that were just read
            # out, a bare ordinal is a choice, not a date. Dropping the date
            # here is what stops "the first one" from being heard as "the 1st"
            # and silently re-running the search.
            if (
                state_hint in _CHOICE_STATES
                and entities.selection_ordinal is not None
                and entities.date_resolution.from_ordinal
            ):
                entities.date_resolution = DateResolution()
        else:
            entities.date_of_birth = _extract_dob(soft)
            entities.mobile = _extract_mobile(soft)
            entities.first_name, entities.last_name = _extract_names(normalised)

        return entities


# --- Identity extraction helpers -------------------------------------------
def _extract_dob(normalised: str) -> date | None:
    """A date of birth must be explicit and in the past.

    Deliberately stricter than the general date parser: "the 21st" is never a
    date of birth, so identity matching cannot be satisfied by a vague phrase.
    """
    match = _DOB.search(normalised)
    if match:
        if match.group(1):
            day, month, year = int(match.group(1)), int(match.group(2)), int(match.group(3))
        else:
            year, month, day = int(match.group(4)), int(match.group(5)), int(match.group(6))
        try:
            parsed = date(year, month, day)
        except ValueError:
            return None
        if date(1900, 1, 1) <= parsed <= date.today():
            return parsed

    # Spoken forms: "14 March 1958" and "the third of March 1985". Callers use
    # word ordinals aloud at least as often as digits.
    month_alt = "|".join(sorted(MONTHS, key=len, reverse=True))
    ordinal_alt = "|".join(sorted(ORDINAL_WORDS, key=len, reverse=True))
    spoken = re.search(
        rf"\b(?:the\s+)?(\d{{1,2}}|{ordinal_alt})(?:st|nd|rd|th)?\s+(?:of\s+)?"
        rf"({month_alt})\s+(\d{{4}})\b",
        normalised,
    )
    if spoken:
        raw_day = spoken.group(1)
        day_number = int(raw_day) if raw_day.isdigit() else ORDINAL_WORDS[raw_day]
        try:
            parsed = date(int(spoken.group(3)), MONTHS[spoken.group(2)], day_number)
        except ValueError:
            return None
        if date(1900, 1, 1) <= parsed <= date.today():
            return parsed
    return None


def _extract_mobile(normalised: str) -> str | None:
    match = _MOBILE.search(normalised)
    if match:
        return re.sub(r"[^\d+]", "", match.group(0))
    # Digits dictated with spaces, e.g. "0 4 1 2 3 4 5 6 7 8".
    digits = re.sub(r"[^\d]", "", normalised)
    if len(digits) == 10 and digits.startswith("04"):
        return digits
    return None


_NAME_STOPWORDS = frozenset(
    {
        "yes",
        "no",
        "the",
        "and",
        "please",
        "thanks",
        "thank",
        "you",
        "is",
        "im",
        "my",
        "name",
        "surname",
        "last",
        "first",
        "family",
        "its",
        "this",
        "that",
        "speaking",
        "calling",
        "here",
        "hello",
        "hi",
        "born",
        "of",
        "on",
        "date",
        "birth",
        "dob",
        # Month names would otherwise be read as a surname when the caller is
        # answering "and your date of birth?" with "fourteenth of March 1958".
        *MONTHS,
    }
)


def _extract_names(normalised: str) -> tuple[str | None, str | None]:
    """Best-effort name extraction from an identification turn.

    Returns ``(first_name, last_name)``; either may be None. This is a
    convenience only — a name alone never satisfies identification, which
    always requires a surname *and* a date of birth.
    """
    surname = _SURNAME_PHRASE.search(normalised)
    if surname:
        return None, _clean_name(surname.group(1))

    full = _FULLNAME_PHRASE.search(normalised)
    if full:
        first = _clean_name(full.group(1))
        last = _clean_name(full.group(2)) if full.group(2) else None
        return first, last

    # A bare one- or two-word reply to "what's your surname?".
    tokens = [t for t in normalised.split() if t not in _NAME_STOPWORDS and t.isalpha()]
    if len(tokens) == 1:
        return None, _clean_name(tokens[0])
    if len(tokens) == 2:
        return _clean_name(tokens[0]), _clean_name(tokens[1])
    return None, None


def _clean_name(value: str | None) -> str | None:
    if not value or len(value) < 2 or value in _NAME_STOPWORDS:
        return None
    return value.strip().title()
