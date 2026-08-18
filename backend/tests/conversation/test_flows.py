"""Reschedule, cancel, information, escalation and repair flows."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.appointments.availability import AvailabilityQuery, AvailabilityService
from app.appointments.service import AppointmentService, BookingRequest
from app.conversation.states import ConversationState, Intent, TransferReason
from app.database.models.appointment import Appointment
from app.database.models.enums import AppointmentStatus

pytestmark = pytest.mark.integration


async def _existing_booking(db, clinic_data, settings, patient_key: str = "hughes"):
    """Give a patient a confirmed appointment to act on."""
    slots = await AvailabilityService(db, settings).search(
        AvailabilityQuery(clinic_id=clinic_data.clinic.id, limit=1), clinic_data.tz
    )
    result = await AppointmentService(db, settings).book(
        BookingRequest(
            clinic_id=clinic_data.clinic.id,
            patient_id=clinic_data.patients[patient_key].id,
            slot_id=slots[0].id,
            is_new_patient=False,
        )
    )
    return result.appointment


async def _identify(caller) -> None:
    await caller.say("Hughes")
    await caller.say("14 March 1958")


# --- Cancellation -----------------------------------------------------------
async def test_cancelling_an_appointment(caller, db, clinic_data, settings) -> None:
    appointment = await _existing_booking(db, clinic_data, settings)
    appointment_id = appointment.id

    await caller.say("I need to cancel my appointment")
    await _identify(caller)

    assert caller.last.state is ConversationState.CANCEL_CONFIRMATION, caller.dump()
    assert "confirm" in caller.last.reply.lower()

    await caller.say("yes")
    assert "cancelled" in caller.last.reply.lower()

    stored = await db.get(Appointment, appointment_id)
    await db.refresh(stored)
    assert stored.status is AppointmentStatus.CANCELLED


async def test_cancellation_requires_explicit_confirmation(
    caller, db, clinic_data, settings
) -> None:
    """A misheard "cancel" must not remove a booking on its own."""
    appointment = await _existing_booking(db, clinic_data, settings)
    appointment_id = appointment.id

    await caller.say("cancel my appointment")
    await _identify(caller)
    await caller.say("no, actually leave it")

    stored = await db.get(Appointment, appointment_id)
    await db.refresh(stored)
    assert stored.status is AppointmentStatus.CONFIRMED


async def test_cancelling_with_no_booking_goes_to_reception(caller) -> None:
    await caller.say("I want to cancel my appointment")
    await _identify(caller)
    assert caller.last.requires_human is True


async def test_a_caller_cannot_cancel_someone_elses_appointment(
    caller, db, clinic_data, settings
) -> None:
    """Nguyen's booking must be untouchable by a caller who identifies as Hughes."""
    other = await _existing_booking(db, clinic_data, settings, patient_key="nguyen")
    other_id = other.id

    await caller.say("cancel my appointment")
    await _identify(caller)  # identifies as Hughes
    await caller.say("yes")

    stored = await db.get(Appointment, other_id)
    await db.refresh(stored)
    assert stored.status is AppointmentStatus.CONFIRMED


# --- Rescheduling -----------------------------------------------------------
async def test_rescheduling_an_appointment(caller, db, clinic_data, settings) -> None:
    appointment = await _existing_booking(db, clinic_data, settings)
    original_id = appointment.id

    await caller.say("I can't make my appointment anymore")
    await _identify(caller)
    assert caller.last.state is ConversationState.RESCHEDULE_SELECTION, caller.dump()

    await caller.say("next Tuesday morning")
    if caller.last.state is ConversationState.RESCHEDULE_SELECTION:
        await caller.say("the first one")
    assert caller.last.state is ConversationState.RESCHEDULE_CONFIRMATION, caller.dump()

    await caller.say("yes")
    assert caller.last.state is ConversationState.BOOKING_COMPLETE, caller.dump()

    old = await db.get(Appointment, original_id)
    await db.refresh(old)
    assert old.status is AppointmentStatus.CANCELLED
    assert old.rescheduled_to_id is not None

    confirmed = (
        (
            await db.execute(
                select(Appointment).where(Appointment.status == AppointmentStatus.CONFIRMED)
            )
        )
        .scalars()
        .all()
    )
    assert len(confirmed) == 1


async def test_reschedule_recognised_from_natural_phrasing(
    caller, db, clinic_data, settings
) -> None:
    await _existing_booking(db, clinic_data, settings)
    await caller.say("can I move my appointment?")
    session = await caller.session()
    assert session.intent is Intent.RESCHEDULE_APPOINTMENT


# --- Clinic information -----------------------------------------------------
@pytest.mark.parametrize(
    ("question", "expected"),
    [
        ("what time do you open?", "weekdays"),
        ("where are you located?", "riverside"),
        ("is there parking?", "parking"),
        ("do you bulk bill?", "bulk bill"),
        ("what's the cancellation policy?", "notice"),
        ("do you do telehealth?", "telephone consultations"),
        ("are you taking new patients?", "new patients"),
        ("is the clinic wheelchair accessible?", "step-free"),
    ],
)
async def test_approved_answers_are_given_verbatim(caller, question, expected) -> None:
    reply = await caller.say(question)
    assert expected.lower() in reply.lower(), reply
    assert caller.last.requires_human is False


async def test_an_unknown_question_transfers_rather_than_guesses(caller) -> None:
    """No approved entry means reception, never an invented answer."""
    reply = await caller.say("do you have an MRI machine on site?")
    assert caller.last.requires_human is True
    assert "reception" in reply.lower()


async def test_test_result_requests_are_handed_over(caller) -> None:
    reply = await caller.say("are my blood test results back?")
    assert caller.last.requires_human is True
    assert "reception" in reply.lower()


async def test_prescription_requests_are_handed_over(caller) -> None:
    await caller.say("I need a repeat prescription")
    assert caller.last.requires_human is True


# --- Escalation and repair --------------------------------------------------
@pytest.mark.parametrize(
    "utterance",
    [
        "let me speak to someone",
        "I want reception",
        "can I talk to a person",
        "put me through to a human",
        "I'd like to speak to a receptionist",
    ],
)
async def test_requests_for_a_human_are_honoured(caller, utterance: str) -> None:
    reply = await caller.say(utterance)
    assert caller.last.requires_human is True
    assert caller.last.state is ConversationState.HUMAN_TRANSFER
    # The bot must never argue or try to keep the caller.
    lowered = reply.lower()
    for pushback in ("are you sure", "i can help", "instead", "before i transfer", "why"):
        assert pushback not in lowered


async def test_a_human_request_mid_booking_wins(caller) -> None:
    await caller.say("I need an appointment")
    await caller.say("existing patient")
    await caller.say("actually just put me through to reception")
    assert caller.last.requires_human is True


async def test_repeated_misunderstanding_escalates(caller, settings) -> None:
    for _ in range(settings.max_misunderstandings + 2):
        await caller.say("mmm hmm")
        if caller.last.requires_human:
            break
    assert caller.last.requires_human is True
    assert caller.last.transfer_reason == TransferReason.REPEATED_MISUNDERSTANDING.value


async def test_a_transferred_call_stays_transferred(caller) -> None:
    await caller.say("put me through to reception")
    assert caller.last.requires_human is True
    # Further input must not re-enter the workflow.
    await caller.say("actually book me an appointment tomorrow")
    assert caller.last.state is ConversationState.HUMAN_TRANSFER


async def test_saying_goodbye_ends_the_call(caller) -> None:
    await caller.say("what time do you open?")
    await caller.say("no thanks, that's all")
    assert caller.last.state is ConversationState.END_CALL
    assert caller.last.ended is True
