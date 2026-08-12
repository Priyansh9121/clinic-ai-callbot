"""Baseline intent classification.

The labelled set below is the **measured baseline**. When Milestone 2 adds an
LLM-backed analyser behind the same ``MessageAnalyser`` interface, it runs
against this same fixture set and must beat these numbers to be worth shipping.
Recording the threshold in an assertion is what stops "the model feels better"
from becoming the acceptance criterion.
"""

from __future__ import annotations

import uuid
from datetime import date

import pytest

from app.conversation.nlu.baseline import (
    AppointmentTypeTerm,
    BaselineAnalyser,
    PractitionerTerm,
    Vocabulary,
)
from app.conversation.states import Intent, PatientType

TODAY = date(2026, 8, 12)

PATEL = uuid.uuid4()
WILSON = uuid.uuid4()
STANDARD = uuid.uuid4()
TELEHEALTH = uuid.uuid4()

VOCABULARY = Vocabulary(
    practitioners=(
        PractitionerTerm(
            PATEL,
            "Dr Sarah Patel",
            ("dr sarah patel", "doctor patel", "sarah patel", "dr patel", "patel"),
        ),
        PractitionerTerm(
            WILSON,
            "Dr James Wilson",
            ("dr james wilson", "doctor wilson", "james wilson", "dr wilson", "wilson"),
        ),
    ),
    appointment_types=(
        AppointmentTypeTerm(
            STANDARD,
            "standard_gp",
            "Standard GP",
            ("standard appointment", "standard gp", "standard", "normal"),
        ),
        AppointmentTypeTerm(
            TELEHEALTH,
            "telehealth",
            "Telehealth",
            ("phone appointment", "telehealth", "over the phone"),
        ),
    ),
)


@pytest.fixture
def analyser() -> BaselineAnalyser:
    return BaselineAnalyser()


async def _analyse(analyser: BaselineAnalyser, text: str, state: str | None = None):
    return await analyser.analyse(text, vocabulary=VOCABULARY, today=TODAY, state_hint=state)


# --- Labelled corpus --------------------------------------------------------
LABELLED: list[tuple[str, Intent]] = [
    # Booking
    ("I need an appointment", Intent.BOOK_APPOINTMENT),
    ("I'd like to book an appointment please", Intent.BOOK_APPOINTMENT),
    ("can I make an appointment", Intent.BOOK_APPOINTMENT),
    ("I want to see a doctor", Intent.BOOK_APPOINTMENT),
    ("can I see Dr Patel on Friday", Intent.BOOK_APPOINTMENT),
    ("anything available tomorrow afternoon", Intent.BOOK_APPOINTMENT),
    ("do you have any appointments free next week", Intent.BOOK_APPOINTMENT),
    ("I need to come in and see the GP", Intent.BOOK_APPOINTMENT),
    ("book me in for a check up", Intent.BOOK_APPOINTMENT),
    ("I'd like to arrange a consultation", Intent.BOOK_APPOINTMENT),
    # Rescheduling
    ("I need to reschedule my appointment", Intent.RESCHEDULE_APPOINTMENT),
    ("can I move my appointment", Intent.RESCHEDULE_APPOINTMENT),
    ("I can't make Friday anymore", Intent.RESCHEDULE_APPOINTMENT),
    ("can I change the time of my appointment", Intent.RESCHEDULE_APPOINTMENT),
    ("I need a different day", Intent.RESCHEDULE_APPOINTMENT),
    ("could we push my appointment back", Intent.RESCHEDULE_APPOINTMENT),
    # Cancellation
    ("I need to cancel my appointment", Intent.CANCEL_APPOINTMENT),
    ("cancel my appointment tomorrow", Intent.CANCEL_APPOINTMENT),
    ("please cancel it", Intent.CANCEL_APPOINTMENT),
    ("I don't need the appointment anymore", Intent.CANCEL_APPOINTMENT),
    # Information
    ("what time do you open", Intent.CLINIC_INFORMATION),
    ("what are your opening hours", Intent.CLINIC_INFORMATION),
    ("do you bulk bill", Intent.CLINIC_INFORMATION),
    ("how much is a consultation", Intent.CLINIC_INFORMATION),
    ("where are you located", Intent.CLINIC_INFORMATION),
    ("is there parking", Intent.CLINIC_INFORMATION),
    ("do you offer telehealth", Intent.CLINIC_INFORMATION),
    ("what's your cancellation policy", Intent.CLINIC_INFORMATION),
    # Human
    ("let me speak to someone", Intent.HUMAN_RECEPTION),
    ("I want reception", Intent.HUMAN_RECEPTION),
    ("can I talk to a person", Intent.HUMAN_RECEPTION),
    ("put me through to a human", Intent.HUMAN_RECEPTION),
    ("I'd like to speak to a receptionist", Intent.HUMAN_RECEPTION),
    # Closing
    ("no thanks that's all", Intent.END_CALL),
    ("nothing else", Intent.END_CALL),
    ("goodbye", Intent.END_CALL),
]

#: The bar the baseline must clear, and that an LLM analyser must beat.
MINIMUM_ACCURACY = 0.90


async def test_baseline_intent_accuracy(analyser: BaselineAnalyser) -> None:
    wrong: list[tuple[str, Intent, Intent]] = []
    for text, expected in LABELLED:
        analysis = await _analyse(analyser, text)
        if analysis.intent is not expected:
            wrong.append((text, expected, analysis.intent))

    accuracy = 1 - len(wrong) / len(LABELLED)
    detail = "\n".join(f"  {t!r}: expected {e.value}, got {g.value}" for t, e, g in wrong)
    assert accuracy >= MINIMUM_ACCURACY, (
        f"baseline accuracy {accuracy:.0%} below {MINIMUM_ACCURACY:.0%}\n{detail}"
    )


@pytest.mark.parametrize(("text", "expected"), LABELLED)
async def test_each_labelled_utterance(
    analyser: BaselineAnalyser, text: str, expected: Intent
) -> None:
    analysis = await _analyse(analyser, text)
    assert analysis.intent is expected, f"{text!r} -> {analysis.intent.value}"


# --- Entity extraction ------------------------------------------------------
async def test_practitioner_resolves_to_an_id(analyser: BaselineAnalyser) -> None:
    for phrasing in ("Dr Patel", "doctor patel", "I want to see Sarah Patel", "patel please"):
        analysis = await _analyse(analyser, phrasing)
        assert analysis.entities.practitioner_id == PATEL, phrasing


async def test_the_longest_matching_name_wins(analyser: BaselineAnalyser) -> None:
    analysis = await _analyse(analyser, "I'd like to see Dr James Wilson")
    assert analysis.entities.practitioner_id == WILSON


async def test_an_unknown_practitioner_yields_no_id(analyser: BaselineAnalyser) -> None:
    """No id means the flow asks again instead of inventing a booking."""
    analysis = await _analyse(analyser, "I want to see Dr Nonexistent")
    assert analysis.entities.practitioner_id is None


@pytest.mark.parametrize(
    "text",
    [
        "any doctor is fine",
        "I don't mind",
        "whoever is available",
        "no preference",
        "doesn't matter",
        "first available",
    ],
)
async def test_no_practitioner_preference(analyser: BaselineAnalyser, text: str) -> None:
    analysis = await _analyse(analyser, text)
    assert analysis.entities.practitioner_any is True


async def test_appointment_type_resolves(analyser: BaselineAnalyser) -> None:
    assert (await _analyse(analyser, "a standard appointment")).entities.appointment_type_id == (
        STANDARD
    )
    assert (await _analyse(analyser, "over the phone please")).entities.appointment_type_id == (
        TELEHEALTH
    )


async def test_patient_type_detection(analyser: BaselineAnalyser) -> None:
    assert (await _analyse(analyser, "I'm a new patient")).entities.patient_type is PatientType.NEW
    assert (
        await _analyse(analyser, "I've been before")
    ).entities.patient_type is PatientType.EXISTING


async def test_combined_entities_in_one_sentence(analyser: BaselineAnalyser) -> None:
    analysis = await _analyse(
        analyser, "can I get a standard appointment with Dr Patel next Friday afternoon"
    )
    entities = analysis.entities
    assert analysis.intent is Intent.BOOK_APPOINTMENT
    assert entities.practitioner_id == PATEL
    assert entities.appointment_type_id == STANDARD
    assert entities.preferred_date == date(2026, 8, 21)
    assert entities.preferred_period.value == "AFTERNOON"


async def test_affirmative_and_negative(analyser: BaselineAnalyser) -> None:
    for yes in ("yes", "yep", "that's right", "sounds good", "go ahead", "perfect"):
        assert (await _analyse(analyser, yes)).affirmative is True, yes
    for no in ("no", "nope", "not really", "that's wrong", "none of those"):
        assert (await _analyse(analyser, no)).negative is True, no


async def test_correction_markers_are_flagged(analyser: BaselineAnalyser) -> None:
    for text in (
        "actually Thursday",
        "sorry I meant Dr Wilson",
        "no wait, afternoon",
        "make that Friday",
    ):
        assert (await _analyse(analyser, text)).is_correction is True, text


async def test_ordinal_selection(analyser: BaselineAnalyser) -> None:
    assert (await _analyse(analyser, "the first one")).entities.selection_ordinal == 1
    assert (await _analyse(analyser, "the second")).entities.selection_ordinal == 2
    assert (await _analyse(analyser, "number two")).entities.selection_ordinal == 2
    assert (await _analyse(analyser, "the last one")).entities.selection_ordinal == -1


async def test_identity_extraction_is_restricted_to_identity_states(
    analyser: BaselineAnalyser,
) -> None:
    """In an identity turn a surname must not be read as a practitioner."""
    analysis = await _analyse(analyser, "Wilson", state="VERIFY_PATIENT")
    assert analysis.entities.practitioner_id is None
    assert analysis.entities.last_name == "Wilson"

    # ...but outside one, the same word is a practitioner.
    analysis = await _analyse(analyser, "Wilson")
    assert analysis.entities.practitioner_id == WILSON


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("14 March 1958", date(1958, 3, 14)),
        ("14/03/1958", date(1958, 3, 14)),
        ("1958-03-14", date(1958, 3, 14)),
        ("the third of March 1985", date(1985, 3, 3)),
    ],
)
async def test_date_of_birth_extraction(
    analyser: BaselineAnalyser, text: str, expected: date
) -> None:
    analysis = await _analyse(analyser, text, state="VERIFY_PATIENT")
    assert analysis.entities.date_of_birth == expected


async def test_vague_dates_are_never_a_date_of_birth(analyser: BaselineAnalyser) -> None:
    """Identity must not be satisfiable with "the 21st"."""
    analysis = await _analyse(analyser, "the 21st", state="VERIFY_PATIENT")
    assert analysis.entities.date_of_birth is None


async def test_mobile_extraction(analyser: BaselineAnalyser) -> None:
    for text in ("0491570156", "0491 570 156", "my number is 0491570156"):
        analysis = await _analyse(analyser, text, state="NEW_PATIENT_DETAILS")
        assert analysis.entities.mobile == "0491570156", text


async def test_empty_utterances_are_marked_empty(analyser: BaselineAnalyser) -> None:
    assert (await _analyse(analyser, "mmm")).is_empty is True
    assert (await _analyse(analyser, "uh huh err")).is_empty is True
    assert (await _analyse(analyser, "tomorrow")).is_empty is False
