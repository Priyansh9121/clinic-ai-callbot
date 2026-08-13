"""Resolve model entity *hints* into the application's own ``Entities``.

This module is the boundary where untrusted model text either becomes a real
database id or becomes nothing at all. Every hint goes through the same
resolvers the deterministic analyser uses — the clinic vocabulary loaded from
the database, and the deterministic date parser bound to the clinic clock.

The consequences, which the tests assert directly:

* "Dr Harry Potter" resolves to ``None``. There is no nearest-match fallback,
  so a hallucinated practitioner cannot become a booking with a real doctor.
* "the premium executive brain scan package" resolves to ``None``.
* "next Friday" is resolved here, in code, against ``today``. A model-supplied
  ISO date is never trusted; only the *phrase* is used.
* A hint containing a UUID, SQL or a path resolves to nothing, because those
  match no vocabulary entry and parse as no date.
"""

from __future__ import annotations

from datetime import date

from app.conversation.nlu.baseline import Entities, Vocabulary, soft_normalise
from app.conversation.nlu.dates import parse_clock_time, resolve_date, resolve_time
from app.conversation.states import PatientType
from app.core.timeutils import DayPeriod
from app.llm.provider import EntityHints
from app.safety.engine import normalise

#: Hints longer than this are not plausible entity mentions. Bounds the work
#: done on adversarial input.
MAX_HINT_CHARS = 80

_PATIENT_TYPES: dict[str, PatientType] = {
    "new": PatientType.NEW,
    "new patient": PatientType.NEW,
    "existing": PatientType.EXISTING,
    "existing patient": PatientType.EXISTING,
    "returning": PatientType.EXISTING,
    "current": PatientType.EXISTING,
}

_PERIODS: dict[str, DayPeriod] = {
    "morning": DayPeriod.MORNING,
    "am": DayPeriod.MORNING,
    "afternoon": DayPeriod.AFTERNOON,
    "pm": DayPeriod.AFTERNOON,
    "evening": DayPeriod.EVENING,
    "night": DayPeriod.EVENING,
    "any": DayPeriod.ANY,
    "anytime": DayPeriod.ANY,
}

# Ordered most-specific first. "the second one" contains both "second" and
# "one"; scanning an unordered mapping matched "one" and selected the first
# slot instead of the second, which would have booked the wrong appointment.
_ORDINAL_PATTERNS: tuple[tuple[tuple[str, ...], int], ...] = (
    (("first", "1st", "earliest", "soonest"), 1),
    (("second", "2nd"), 2),
    (("third", "3rd"), 3),
    (("fourth", "4th"), 4),
    (("last", "latest", "later", "final"), -1),
)

#: Only accepted when the hint is *exactly* one of these, so a bare "one" is a
#: choice but the "one" inside "the second one" is not.
_EXACT_ORDINALS: dict[str, int] = {
    "one": 1,
    "1": 1,
    "two": 2,
    "2": 2,
    "three": 3,
    "3": 3,
    "four": 4,
    "4": 4,
}

#: Surface forms that legitimately express a category the caller did not say
#: verbatim. Used by the grounding check so "arvo" can ground "afternoon"
#: while an invented "morning" is still rejected.
_GROUNDING_SYNONYMS: dict[str, tuple[str, ...]] = {
    "morning": ("morning", "am", "early", "first thing", "before lunch", "before noon"),
    "afternoon": ("afternoon", "pm", "arvo", "after lunch", "midday", "lunchtime"),
    "evening": ("evening", "tonight", "night", "after work", "late"),
    "new": ("new", "first time", "never been", "havent been", "not been", "never visited"),
    "existing": (
        "existing",
        "been before",
        "been there",
        "regular",
        "current",
        "already",
        "ive been",
        "i have been",
        "patient there",
        "returning",
    ),
}

#: Words too generic to prove a hint came from the caller.
_GROUNDING_STOPWORDS = frozenset(
    {"the", "a", "an", "dr", "doctor", "one", "appointment", "please", "my", "and", "for"}
)


def _clean(hint: str | None) -> str | None:
    if not hint:
        return None
    trimmed = hint.strip()[:MAX_HINT_CHARS]
    return trimmed or None


def is_grounded(hint: str, utterance_normalised: str) -> bool:
    """Whether the hint is actually traceable to something the caller said.

    The prompt instructs the model to copy entity values from the caller's own
    words, so a hint with no basis in the utterance is a hallucination. This
    matters because hints are *overlaid* onto the deterministic result: without
    this check, a model that invented ``preferred_period="morning"`` for the
    utterance "the second one" would overwrite a correct value and, worse, look
    to the flow like a deliberate correction — discarding the slots that had
    just been offered.

    Observed in practice with qwen2.5:1.5b, which is why it exists.

    A hint grounds if any of its meaningful tokens, or any accepted synonym for
    it, appears in the normalised utterance.
    """
    if not hint or not utterance_normalised:
        return False
    normalised_hint = normalise(hint)
    if not normalised_hint:
        return False

    haystack = utterance_normalised
    if normalised_hint in haystack:
        return True

    for synonym in _GROUNDING_SYNONYMS.get(normalised_hint, ()):
        if synonym in haystack:
            return True

    tokens = [
        token
        for token in normalised_hint.split()
        if len(token) >= 3 and token not in _GROUNDING_STOPWORDS
    ]
    # Substring rather than token equality so "patel" grounds in "patels".
    return any(token in haystack for token in tokens)


def resolve_hints(
    hints: EntityHints,
    *,
    vocabulary: Vocabulary,
    today: date,
    into: Entities | None = None,
    utterance_normalised: str = "",
) -> Entities:
    """Resolve hints into ``into`` (usually the deterministic result).

    **Hints fill gaps; they never contradict.** A field is written only when the
    deterministic analyser left it empty *and* the hint both grounds in the
    utterance and resolves to something real. That makes the merge strictly
    additive, so the combined result cannot be worse than the deterministic
    analyser alone.

    The rule was tightened after measuring. Given "Not morning, afternoon.",
    qwen2.5:1.5b returns ``preferred_period="morning"`` — which grounds, because
    the word genuinely appears — and overwrote the baseline's correct
    AFTERNOON. Grounding cannot discriminate when both candidate values are
    present in the utterance, so the model does not get to overrule a
    deterministic parse at all.

    ``utterance_normalised`` enables the grounding check. It defaults to empty
    for callers that only want resolution, but the analyser always passes it.
    """
    entities = into if into is not None else Entities()

    def grounded(hint: str | None) -> str | None:
        """None unless the hint is traceable to the caller's own words."""
        cleaned = _clean(hint)
        if cleaned is None:
            return None
        if utterance_normalised and not is_grounded(cleaned, utterance_normalised):
            return None
        return cleaned

    # Practitioner and appointment type resolve against the clinic vocabulary,
    # so they are grounded against the *matched term's* aliases rather than the
    # hint text. A model answering "phone appointment" with the hint
    # "Telehealth" is normalising, not hallucinating, and both are aliases of
    # the same record — whereas a name that matches no record is rejected by
    # the vocabulary lookup regardless.
    _resolve_practitioner(_clean(hints.practitioner), vocabulary, entities, utterance_normalised)
    _resolve_appointment_type(
        _clean(hints.appointment_type), vocabulary, entities, utterance_normalised
    )
    _resolve_patient_type(grounded(hints.patient_type), entities)
    _resolve_date(grounded(hints.preferred_date_text), today, entities)
    _resolve_period(grounded(hints.preferred_period), entities)
    _resolve_selection(grounded(hints.selection), entities)

    return entities


def _term_is_grounded(aliases: tuple[str, ...], utterance_normalised: str) -> bool:
    """Whether any surface form of a matched record appears in the utterance."""
    if not utterance_normalised:
        return True
    return any(
        alias in utterance_normalised
        for alias in aliases
        if len(alias) >= 3 and alias not in _GROUNDING_STOPWORDS
    )


def _resolve_practitioner(
    hint: str | None,
    vocabulary: Vocabulary,
    entities: Entities,
    utterance_normalised: str = "",
) -> None:
    if hint is None:
        return
    normalised = normalise(hint)

    # "anyone", "no preference" is a real answer, not a name. Exempt from
    # grounding: it can only widen the search, and it never displaces a
    # practitioner the deterministic layer already found.
    if any(
        token in normalised
        for token in (
            "any",
            "anyone",
            "whoever",
            "whichever",
            "no preference",
            "dont mind",
            "doesnt matter",
            "first available",
        )
    ):
        if entities.practitioner_id is None:
            entities.practitioner_any = True
        return

    if entities.practitioner_id is not None:
        return  # the deterministic analyser already decided

    term = vocabulary.find_practitioner(normalised)
    if term is None:
        # Unknown name. Nothing is set, so the flow asks again rather than
        # booking with whoever happens to be closest alphabetically.
        return
    if not _term_is_grounded(term.aliases, utterance_normalised):
        # Resolves to a real doctor the caller never mentioned.
        return
    entities.practitioner_id = term.practitioner_id
    entities.practitioner_name = term.display_name
    entities.practitioner_any = False


def _resolve_appointment_type(
    hint: str | None,
    vocabulary: Vocabulary,
    entities: Entities,
    utterance_normalised: str = "",
) -> None:
    if hint is None or entities.appointment_type_id is not None:
        return
    term = vocabulary.find_appointment_type(normalise(hint))
    if term is None:
        return  # a service this clinic does not offer resolves to nothing
    if not _term_is_grounded(term.aliases, utterance_normalised):
        return
    entities.appointment_type_id = term.appointment_type_id
    entities.appointment_type_name = term.name


def _resolve_patient_type(hint: str | None, entities: Entities) -> None:
    if hint is None or entities.patient_type is not None:
        return
    normalised = normalise(hint)
    mapped = _PATIENT_TYPES.get(normalised)
    if mapped is None:
        # Substring match for "I'm an existing patient" style hints.
        if "new" in normalised:
            mapped = PatientType.NEW
        elif "exist" in normalised or "return" in normalised:
            mapped = PatientType.EXISTING
    if mapped is not None:
        entities.patient_type = mapped


def _resolve_date(hint: str | None, today: date, entities: Entities) -> None:
    """Resolve the *phrase*, never a model-supplied date value.

    The same deterministic parser the baseline uses, bound to the clinic's
    today. An ISO date emitted by the model is only honoured if it parses here
    and survives the parser's own validation (impossible dates, past dates
    rolling forward, and so on).
    """
    if hint is None or not entities.date_resolution.is_empty:
        return
    resolution = resolve_date(soft_normalise(hint), today)
    if resolution.is_empty:
        return
    entities.date_resolution = resolution


def _resolve_period(hint: str | None, entities: Entities) -> None:
    if hint is None or not entities.time_resolution.is_empty:
        return
    normalised = normalise(hint)
    period = _PERIODS.get(normalised)
    if period is None:
        # Fall back to the deterministic time parser, which also picks up
        # "after lunch", "first thing" and "after 3".
        resolution = resolve_time(soft_normalise(hint))
        if resolution.is_empty:
            return
        entities.time_resolution = resolution
        return
    if period is DayPeriod.ANY:
        return
    existing = entities.time_resolution
    entities.time_resolution = type(existing)(
        period=period, not_before=existing.not_before, expression=normalised
    )


def _resolve_selection(hint: str | None, entities: Entities) -> None:
    """Resolve "the first one" / "2:30" into an ordinal or a clock time.

    The flow then matches that against the slots it actually offered, so a
    selection can only ever pick from what was read out.
    """
    if hint is None:
        return
    normalised = normalise(hint)
    tokens = set(normalised.split())

    ordinal = _EXACT_ORDINALS.get(normalised)
    if ordinal is None:
        # Most specific first, so "the second one" is the second option.
        for words, value in _ORDINAL_PATTERNS:
            if tokens.intersection(words):
                ordinal = value
                break
    if ordinal is not None and entities.selection_ordinal is None:
        entities.selection_ordinal = ordinal

    if entities.selection_time is None:
        clock = parse_clock_time(soft_normalise(hint))
        if clock is not None:
            entities.selection_time = clock
