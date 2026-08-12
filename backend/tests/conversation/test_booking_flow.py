"""End-to-end conversations through the real engine."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import func, select

from app.conversation.states import ConversationState, Intent, VerificationLevel
from app.core.timeutils import utc_now
from app.database.models.appointment import Appointment
from app.database.models.enums import AppointmentStatus, BookingSource

pytestmark = pytest.mark.integration

HUGHES = ("Hughes", "14 March 1958")


async def _identify(caller) -> None:
    await caller.say("existing patient")
    await caller.say(HUGHES[0])
    await caller.say(HUGHES[1])


async def test_greeting_discloses_that_it_is_automated(caller) -> None:
    """Disclosure is a requirement, not a nicety."""
    assert "automated" in caller.transcript[0][1].lower()


async def test_a_complete_booking_commits_an_appointment(caller, db) -> None:
    await caller.say("I'd like to book an appointment")
    await _identify(caller)
    await caller.say("standard appointment")
    await caller.say("Dr Patel")
    reply = await caller.say("tomorrow")

    assert caller.last.state in (
        ConversationState.SELECT_SLOT,
        ConversationState.CONFIRM_BOOKING,
        ConversationState.PREFERRED_PERIOD,
    ), caller.dump()

    if caller.last.state is ConversationState.PREFERRED_PERIOD:
        reply = await caller.say("morning")
    if caller.last.state is ConversationState.SELECT_SLOT:
        reply = await caller.say("the first one")
    assert caller.last.state is ConversationState.CONFIRM_BOOKING, caller.dump()
    assert "shall i book" in reply.lower() or "does that suit" in reply.lower()

    reply = await caller.say("yes please")
    assert caller.last.state is ConversationState.BOOKING_COMPLETE, caller.dump()
    assert "booked" in reply.lower()

    count = await db.scalar(
        select(func.count())
        .select_from(Appointment)
        .where(Appointment.status == AppointmentStatus.CONFIRMED)
    )
    assert count == 1

    appointment = (await db.execute(select(Appointment))).scalars().first()
    assert appointment.booking_source is BookingSource.AI_CALLBOT


async def test_volunteered_details_skip_the_matching_questions(caller) -> None:
    """One rich sentence should not be re-asked field by field."""
    await caller.say("I need a standard appointment with Dr Patel tomorrow afternoon")
    await _identify(caller)

    session = await caller.session()
    assert session.booking.practitioner_id is not None
    assert session.booking.preferred_date is not None
    assert session.booking.appointment_type_id is not None
    # Straight to options: no practitioner/date/type question was needed.
    assert session.state in (
        ConversationState.SELECT_SLOT,
        ConversationState.CONFIRM_BOOKING,
    ), caller.dump()


async def test_no_practitioner_preference_is_accepted(caller) -> None:
    await caller.say("I need an appointment")
    await _identify(caller)
    await caller.say("standard")
    await caller.say("I don't mind which doctor")

    session = await caller.session()
    assert session.booking.practitioner_any is True
    assert session.booking.practitioner_id is None


async def test_identification_is_required_before_booking(caller) -> None:
    await caller.say("book me an appointment for tomorrow")
    session = await caller.session()
    assert session.verification_level is VerificationLevel.NONE
    assert session.state in (
        ConversationState.PATIENT_TYPE,
        ConversationState.VERIFY_PATIENT,
    )


async def test_unknown_details_never_reveal_whether_a_patient_exists(caller) -> None:
    await caller.say("I need an appointment")
    await caller.say("existing patient")
    await caller.say("Zzzznonexistent")
    reply = await caller.say("1 January 1970")

    lowered = reply.lower()
    assert "couldn't verify" in lowered
    # Must not confirm or deny the existence of a record.
    assert "no patient" not in lowered
    assert "not found" not in lowered
    assert "does not exist" not in lowered


async def test_repeated_identification_failure_transfers_to_reception(caller) -> None:
    await caller.say("I need an appointment")
    await caller.say("existing patient")
    for _ in range(2):
        await caller.say("Zzzznonexistent")
        await caller.say("1 January 1970")

    assert caller.last.requires_human is True
    assert caller.last.state is ConversationState.HUMAN_TRANSFER
    assert "reception" in caller.last.reply.lower()


async def test_a_new_patient_can_book(caller, db) -> None:
    await caller.say("I'd like to make an appointment")
    await caller.say("I'm a new patient")
    await caller.say("my name is Alex Turner")
    await caller.say("3 June 1990")
    await caller.say("0491570161")
    await caller.say("next Monday")

    if caller.last.state is ConversationState.PREFERRED_PERIOD:
        await caller.say("morning")
    if caller.last.state is ConversationState.SELECT_SLOT:
        await caller.say("the first one")
    assert caller.last.state is ConversationState.CONFIRM_BOOKING, caller.dump()

    await caller.say("yes")
    assert caller.last.state is ConversationState.BOOKING_COMPLETE, caller.dump()

    count = await db.scalar(select(func.count()).select_from(Appointment))
    assert count == 1


async def test_an_abandoned_new_patient_call_creates_no_patient_record(caller, db) -> None:
    """Records are only created at the point of booking."""
    from app.database.models.patient import Patient

    before = await db.scalar(select(func.count()).select_from(Patient))
    await caller.say("I'd like an appointment")
    await caller.say("I'm new")
    await caller.say("my name is Alex Turner")
    await caller.say("3 June 1990")

    after = await db.scalar(select(func.count()).select_from(Patient))
    assert after == before


async def test_booking_is_not_claimed_before_the_caller_confirms(caller, db) -> None:
    await caller.say("appointment please")
    await _identify(caller)
    await caller.say("standard")
    await caller.say("any doctor")
    await caller.say("tomorrow")
    if caller.last.state is ConversationState.PREFERRED_PERIOD:
        await caller.say("morning")

    # Options offered but not yet accepted — nothing may be committed.
    assert await db.scalar(select(func.count()).select_from(Appointment)) == 0
    assert "booked" not in caller.last.reply.lower()


async def test_declining_the_offer_asks_for_another_day(caller) -> None:
    await caller.say("appointment please")
    await _identify(caller)
    await caller.say("standard")
    await caller.say("any doctor")
    await caller.say("tomorrow")
    if caller.last.state is ConversationState.PREFERRED_PERIOD:
        await caller.say("morning")

    await caller.say("no, none of those")
    session = await caller.session()
    assert session.booking.selected_slot_id is None
    assert session.state is ConversationState.PREFERRED_DATE


async def test_slots_offered_are_never_in_the_past(caller, db) -> None:
    from app.database.models.availability import AvailabilitySlot

    await caller.say("appointment please")
    await _identify(caller)
    await caller.say("standard")
    await caller.say("any doctor")
    await caller.say("tomorrow")
    if caller.last.state is ConversationState.PREFERRED_PERIOD:
        await caller.say("morning")

    session = await caller.session()
    offered = [
        *session.booking.offered_slot_ids,
        *([session.booking.selected_slot_id] if session.booking.selected_slot_id else []),
    ]
    assert offered, caller.dump()

    # Checked against the database, not the spoken text.
    now = utc_now()
    for slot_id in offered:
        slot = await db.get(AvailabilitySlot, slot_id)
        assert slot is not None and slot.start_time > now


async def test_replies_stay_short_enough_to_speak(caller) -> None:
    """Voice turns must be one or two sentences."""
    await caller.say("I need an appointment")
    await _identify(caller)
    await caller.say("standard")
    await caller.say("any doctor")
    await caller.say("tomorrow")

    for who, text in caller.transcript:
        if who != "bot":
            continue
        assert len(text) <= 220, f"reply too long for speech: {text}"
        assert text.count("!") == 0, f"over-enthusiastic reply: {text}"


async def test_intent_is_recorded_on_the_session(caller) -> None:
    await caller.say("I want to book an appointment")
    session = await caller.session()
    assert session.intent is Intent.BOOK_APPOINTMENT
    assert session.intent_confidence > 0


async def test_conversation_memory_excludes_patient_details(caller) -> None:
    await caller.say("I need an appointment")
    await _identify(caller)
    memory = caller.last.memory

    rendered = str(memory).lower()
    assert "hughes" not in rendered
    assert "margaret" not in rendered
    assert "1958" not in rendered
    assert memory["identified"] is True


async def test_lead_time_prevents_booking_something_imminent(
    caller, db, clinic_data, settings
) -> None:
    """A slot inside the lead time must never be offered."""
    from app.appointments.availability import AvailabilityQuery, AvailabilityService

    slots = await AvailabilityService(db, settings).search(
        AvailabilityQuery(clinic_id=clinic_data.clinic.id, limit=100), clinic_data.tz
    )
    earliest_allowed = utc_now() + timedelta(minutes=settings.booking_lead_minutes)
    assert all(slot.start_time >= earliest_allowed for slot in slots)
