"""Entity-hint resolution — the boundary where model text becomes an id, or nothing.

These are the tests that hold the central Milestone 2 claim: the model
understands, the application decides. A hint is only ever a string until this
module matches it against real clinic records and the real clock.
"""

from __future__ import annotations

import uuid
from datetime import date, time

import pytest

from app.conversation.nlu.baseline import (
    AppointmentTypeTerm,
    Entities,
    PractitionerTerm,
    Vocabulary,
)
from app.conversation.nlu.resolution import is_grounded, resolve_hints
from app.conversation.states import PatientType
from app.core.timeutils import DayPeriod
from app.llm.provider import EntityHints

TODAY = date(2026, 8, 12)  # Wednesday

PATEL, WILSON, CHEN = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
STANDARD, LONG, TELEHEALTH = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()

VOCABULARY = Vocabulary(
    practitioners=(
        PractitionerTerm(
            PATEL,
            "Dr Sarah Patel",
            ("dr sarah patel", "doctor patel", "sarah patel", "dr patel", "patel"),
        ),
        PractitionerTerm(
            WILSON, "Dr James Wilson", ("dr james wilson", "doctor wilson", "dr wilson", "wilson")
        ),
        PractitionerTerm(
            CHEN, "Dr Emily Chen", ("dr emily chen", "doctor chen", "emily chen", "dr chen", "chen")
        ),
    ),
    appointment_types=(
        AppointmentTypeTerm(
            STANDARD,
            "standard_gp",
            "Standard GP",
            ("standard appointment", "standard gp", "standard"),
        ),
        AppointmentTypeTerm(LONG, "long_gp", "Long GP", ("long appointment", "long gp", "long")),
        AppointmentTypeTerm(
            TELEHEALTH, "telehealth", "Telehealth", ("telehealth", "phone appointment")
        ),
    ),
)


def resolve(utterance: str, **hints: str | None) -> Entities:
    return resolve_hints(
        EntityHints(**hints),
        vocabulary=VOCABULARY,
        today=TODAY,
        utterance_normalised=utterance,
    )


# --- Practitioner -----------------------------------------------------------
@pytest.mark.parametrize(
    ("hint", "utterance", "expected"),
    [
        ("Dr Patel", "id like to see dr patel", PATEL),
        ("Patel", "any chance patels around friday", PATEL),
        ("Dr Sarah Patel", "book me with dr sarah patel", PATEL),
        ("Wilson", "wilson if hes about", WILSON),
        ("Dr Emily Chen", "can i see dr emily chen", CHEN),
    ],
)
def test_known_practitioners_resolve_to_ids(hint, utterance, expected) -> None:
    assert resolve(utterance, practitioner=hint).practitioner_id == expected


@pytest.mark.parametrize(
    "hint",
    [
        "Dr Harry Potter",
        "Dr Nonexistent",
        "Doctor Who",
        "Dr Smith",
        "the good one",
    ],
)
def test_hallucinated_practitioners_resolve_to_nothing(hint: str) -> None:
    """No nearest-match fallback. An invented doctor produces no id at all,
    so the flow asks again rather than booking with somebody else."""
    entities = resolve(f"can i see {hint.lower()}", practitioner=hint)
    assert entities.practitioner_id is None
    assert entities.practitioner_name is None


@pytest.mark.parametrize(
    ("hint", "utterance"),
    [
        ("anyone", "anyone is fine"),
        ("any doctor", "any doctor is fine"),
        ("no preference", "no preference really"),
        ("whoever is free", "whoever is free is fine"),
    ],
)
def test_no_practitioner_preference_is_understood(hint: str, utterance: str) -> None:
    entities = resolve(utterance, practitioner=hint)
    assert entities.practitioner_any is True
    assert entities.practitioner_id is None


# --- Appointment type -------------------------------------------------------
def test_known_appointment_types_resolve() -> None:
    assert resolve("a standard appointment", appointment_type="standard").appointment_type_id == (
        STANDARD
    )


def test_a_normalised_type_name_grounds_via_its_aliases() -> None:
    """The caller said "phone appointment"; the model answered "Telehealth".

    Both are surface forms of the same record, so this is normalisation rather
    than hallucination and must be accepted.
    """
    entities = resolve("id like a phone appointment", appointment_type="telehealth")
    assert entities.appointment_type_id == TELEHEALTH


def test_a_real_type_the_caller_never_mentioned_is_rejected() -> None:
    entities = resolve("i need an appointment tomorrow", appointment_type="Telehealth")
    assert entities.appointment_type_id is None


@pytest.mark.parametrize(
    "hint",
    ["premium executive brain scan package", "full body MRI", "dental cleaning", "physiotherapy"],
)
def test_services_the_clinic_does_not_offer_resolve_to_nothing(hint: str) -> None:
    entities = resolve(f"i need the {hint}", appointment_type=hint)
    assert entities.appointment_type_id is None
    assert entities.appointment_type_name is None


# --- Dates ------------------------------------------------------------------
@pytest.mark.parametrize(
    ("hint", "expected"),
    [
        ("tomorrow", date(2026, 8, 13)),
        ("next Friday", date(2026, 8, 21)),
        ("Friday", date(2026, 8, 14)),
        ("the 21st", date(2026, 8, 21)),
        ("day after tomorrow", date(2026, 8, 14)),
    ],
)
def test_date_phrases_resolve_deterministically(hint: str, expected: date) -> None:
    """The *phrase* is resolved here, against the clinic clock."""
    entities = resolve(hint.lower(), preferred_date_text=hint)
    assert entities.preferred_date == expected


def test_a_model_supplied_iso_date_is_still_validated() -> None:
    """An ISO date is only honoured if the deterministic parser accepts it."""
    # Grounding needs the phrase in the utterance, as it would be if the caller
    # actually said a date.
    assert resolve("2026-08-21", preferred_date_text="2026-08-21").preferred_date == date(
        2026, 8, 21
    )
    # An impossible date parses to nothing rather than being trusted.
    assert resolve("2026-02-31", preferred_date_text="2026-02-31").preferred_date is None


def test_nonsense_date_hints_resolve_to_nothing() -> None:
    for hint in ("sometime", "whenever suits", "the usual"):
        assert resolve(hint, preferred_date_text=hint).preferred_date is None


# --- Period -----------------------------------------------------------------
@pytest.mark.parametrize(
    ("hint", "utterance", "expected"),
    [
        ("morning", "friday morning", DayPeriod.MORNING),
        ("afternoon", "friday afternoon", DayPeriod.AFTERNOON),
        ("evening", "friday evening", DayPeriod.EVENING),
        ("afternoon", "friday arvo", DayPeriod.AFTERNOON),  # synonym grounding
        ("afternoon", "anything after lunch", DayPeriod.AFTERNOON),
    ],
)
def test_period_hints_resolve(hint, utterance, expected) -> None:
    assert resolve(utterance, preferred_period=hint).preferred_period is expected


def test_after_three_keeps_the_time_bound() -> None:
    entities = resolve("anything after 3", preferred_period="after 3")
    assert entities.not_before == time(15, 0)


# --- Grounding --------------------------------------------------------------
def test_an_ungrounded_hint_is_discarded() -> None:
    """Regression: qwen2.5:1.5b answered "the second one" with
    ``preferred_period="morning"``. Overlaying that would have overwritten a
    correct value and looked to the flow like a deliberate correction,
    discarding the slots that had just been offered."""
    entities = resolve("the second one", preferred_period="morning")
    assert entities.preferred_period is DayPeriod.ANY


def test_an_ungrounded_practitioner_is_discarded() -> None:
    entities = resolve("i need an appointment tomorrow", practitioner="Dr Patel")
    assert entities.practitioner_id is None


def test_grounding_accepts_synonyms_and_word_variants() -> None:
    assert is_grounded("afternoon", "any chance friday arvo") is True
    assert is_grounded("Dr Patel", "any chance patels around") is True
    assert is_grounded("existing", "ive been there before") is True
    assert is_grounded("new", "never been to your clinic") is True
    assert is_grounded("morning", "the second one") is False
    assert is_grounded("Dr Wilson", "i want to see patel") is False


def test_grounding_is_skipped_when_no_utterance_is_supplied() -> None:
    """Resolution stays usable on its own; only the analyser grounds."""
    entities = resolve_hints(
        EntityHints(practitioner="Dr Patel"), vocabulary=VOCABULARY, today=TODAY
    )
    assert entities.practitioner_id == PATEL


# --- Patient type and selection ---------------------------------------------
def test_patient_type_resolves() -> None:
    assert resolve("im a new patient", patient_type="new").patient_type is PatientType.NEW
    assert (
        resolve("ive been there before", patient_type="existing").patient_type
        is PatientType.EXISTING
    )


@pytest.mark.parametrize(
    ("hint", "expected"),
    [
        ("the first one", 1),
        ("first", 1),
        ("the second one", 2),
        ("second", 2),
        ("third", 3),
        ("the last one", -1),
        ("the later one", -1),
        ("earliest", 1),
    ],
)
def test_selection_hints_resolve_to_ordinals(hint: str, expected: int) -> None:
    assert resolve(hint, selection=hint).selection_ordinal == expected


def test_selection_time_resolves() -> None:
    # The analyser passes the *normalised* utterance, which has punctuation
    # stripped, so grounding compares like with like.
    assert resolve("the 2 30 one", selection="2:30").selection_time == time(14, 30)


# --- Adversarial hints ------------------------------------------------------
@pytest.mark.parametrize(
    "hint",
    [
        "00000000-0000-0000-0000-000000000001",
        "'; DROP TABLE appointments; --",
        "SELECT * FROM patients",
        "/api/appointments",
        "book_appointment()",
        "../../etc/passwd",
        "<script>alert(1)</script>",
    ],
)
def test_adversarial_hints_resolve_to_nothing(hint: str) -> None:
    """Identifiers, SQL and paths match no vocabulary entry and parse as no
    date, so they resolve to nothing rather than becoming actionable."""
    entities = resolve_hints(
        # model_construct bypasses field validation, simulating the worst case
        # where a hostile value somehow reaches resolution.
        EntityHints.model_construct(
            practitioner=hint,
            appointment_type=hint,
            preferred_date_text=hint,
            preferred_period=hint,
            patient_type=hint,
            selection=hint,
        ),
        vocabulary=VOCABULARY,
        today=TODAY,
        utterance_normalised=hint.lower(),
    )
    assert entities.practitioner_id is None
    assert entities.appointment_type_id is None
    assert entities.preferred_date is None
    assert entities.patient_type is None


def test_the_schema_rejects_over_long_hints() -> None:
    """First line of defence: the field length bound."""
    import pydantic

    with pytest.raises(pydantic.ValidationError):
        EntityHints(practitioner="x" * 500)
    with pytest.raises(pydantic.ValidationError):
        EntityHints(selection="x" * 200)


def test_resolution_truncates_hints_that_bypass_validation() -> None:
    """Second line: even a hint that skipped validation is bounded."""
    entities = resolve_hints(
        EntityHints.model_construct(practitioner="Dr Patel " + "x" * 5000),
        vocabulary=VOCABULARY,
        today=TODAY,
        utterance_normalised="dr patel",
    )
    # Truncated to MAX_HINT_CHARS, so "Dr Patel" survives and still resolves.
    assert entities.practitioner_id == PATEL


def test_a_hint_never_clears_an_existing_value() -> None:
    """A hint that resolves to nothing leaves the deterministic value alone."""
    existing = Entities(practitioner_id=WILSON, practitioner_name="Dr James Wilson")
    merged = resolve_hints(
        EntityHints(practitioner="Dr Harry Potter"),
        vocabulary=VOCABULARY,
        today=TODAY,
        into=existing,
        utterance_normalised="can i see dr harry potter",
    )
    assert merged.practitioner_id == WILSON


# --- Hints fill gaps, they never contradict ---------------------------------
def test_a_hint_never_overrules_a_deterministic_parse() -> None:
    """Regression, measured: given "Not morning, afternoon.", qwen2.5:1.5b
    returns ``preferred_period="morning"``. It grounds — the word really is in
    the utterance — so grounding alone cannot reject it. The model therefore
    does not get to overrule a value the deterministic parser already produced.
    """
    from app.conversation.nlu.dates import TimeResolution

    existing = Entities(time_resolution=TimeResolution(period=DayPeriod.AFTERNOON))
    merged = resolve_hints(
        EntityHints(preferred_period="morning"),
        vocabulary=VOCABULARY,
        today=TODAY,
        into=existing,
        utterance_normalised="not morning afternoon",
    )
    assert merged.preferred_period is DayPeriod.AFTERNOON


def test_a_hint_fills_a_gap_the_deterministic_parser_missed() -> None:
    """The other half of the rule — this is where the model adds value."""
    merged = resolve_hints(
        EntityHints(preferred_date_text="tomorrow"),
        vocabulary=VOCABULARY,
        today=TODAY,
        into=Entities(),  # baseline found nothing
        utterance_normalised="cancel tomorrows one",
    )
    assert merged.preferred_date == date(2026, 8, 13)


@pytest.mark.parametrize(
    ("field", "hint", "existing"),
    [
        ("practitioner_id", {"practitioner": "Dr Patel"}, Entities(practitioner_id=WILSON)),
        (
            "appointment_type_id",
            {"appointment_type": "long"},
            Entities(appointment_type_id=STANDARD),
        ),
    ],
)
def test_vocabulary_fields_also_fill_gaps_only(field, hint, existing) -> None:
    merged = resolve_hints(
        EntityHints(**hint),
        vocabulary=VOCABULARY,
        today=TODAY,
        into=existing,
        utterance_normalised="dr patel long appointment",
    )
    assert getattr(merged, field) == getattr(existing, field)
