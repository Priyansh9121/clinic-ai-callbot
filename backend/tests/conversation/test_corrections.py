"""Natural corrections.

A caller changing their mind mid-sentence is normal on the phone. The
requirement is that only the corrected field changes: the workflow must not
restart, and settled facts (identity, patient type) must survive.
"""

from __future__ import annotations

import pytest

from app.conversation.states import ConversationState, Intent, VerificationLevel

pytestmark = pytest.mark.integration


async def _identified_booker(caller):
    await caller.say("I'd like to book an appointment")
    await caller.say("existing patient")
    await caller.say("Hughes")
    await caller.say("14 March 1958")
    await caller.say("standard")
    return caller


async def test_changing_the_day_does_not_restart_the_conversation(caller) -> None:
    await _identified_booker(caller)
    await caller.say("Dr Patel")
    await caller.say("next Friday")

    session = await caller.session()
    friday = session.booking.preferred_date

    await caller.say("actually, make it Thursday")
    session = await caller.session()

    assert session.booking.preferred_date != friday
    # Identity and practitioner survive the correction.
    assert session.verification_level is VerificationLevel.IDENTIFIED
    assert session.booking.practitioner_id is not None
    assert session.state is not ConversationState.PATIENT_TYPE
    assert session.state is not ConversationState.VERIFY_PATIENT


async def test_changing_the_day_invalidates_the_offered_slots(caller) -> None:
    """Offers made for Friday must not stay selectable after "Thursday"."""
    await _identified_booker(caller)
    await caller.say("any doctor")
    await caller.say("next Friday")
    if caller.last.state is ConversationState.PREFERRED_PERIOD:
        await caller.say("afternoon")

    session = await caller.session()
    before = list(session.booking.offered_slot_ids)
    assert before, caller.dump()

    await caller.say("actually Thursday")
    session = await caller.session()
    assert session.booking.offered_slot_ids != before


async def test_changing_the_period_re_searches(caller) -> None:
    await _identified_booker(caller)
    await caller.say("any doctor")
    await caller.say("tomorrow morning")
    session = await caller.session()
    assert session.booking.preferred_period.value == "MORNING"

    await caller.say("no, afternoon")
    session = await caller.session()
    assert session.booking.preferred_period.value == "AFTERNOON"


async def test_changing_the_practitioner_keeps_the_date(caller) -> None:
    await _identified_booker(caller)
    await caller.say("Dr Patel")
    await caller.say("next Tuesday")
    session = await caller.session()
    chosen_day = session.booking.preferred_date
    first_doctor = session.booking.practitioner_id

    await caller.say("sorry, I meant Dr Wilson")
    session = await caller.session()

    assert session.booking.practitioner_id != first_doctor
    assert session.booking.preferred_date == chosen_day


async def test_dropping_a_practitioner_preference_mid_flow(caller) -> None:
    await _identified_booker(caller)
    await caller.say("Dr Patel")
    await caller.say("next Tuesday")

    await caller.say("actually any doctor is fine")
    session = await caller.session()
    assert session.booking.practitioner_any is True
    assert session.booking.practitioner_id is None


async def test_correcting_after_selecting_a_slot_reopens_the_search(caller) -> None:
    await _identified_booker(caller)
    await caller.say("any doctor")
    await caller.say("tomorrow")
    if caller.last.state is ConversationState.PREFERRED_PERIOD:
        await caller.say("morning")
    if caller.last.state is ConversationState.SELECT_SLOT:
        await caller.say("the first one")
    assert caller.last.state is ConversationState.CONFIRM_BOOKING, caller.dump()

    session = await caller.session()
    previously_selected = session.booking.selected_slot_id
    assert previously_selected is not None

    await caller.say("actually can we do next Monday instead")
    session = await caller.session()

    assert session.state in (
        ConversationState.SELECT_SLOT,
        ConversationState.CONFIRM_BOOKING,
        ConversationState.PREFERRED_PERIOD,
    )
    # Whatever happens next, the slot chosen under the old preference must not
    # still be sitting there ready to be booked by a stray "yes".
    assert session.booking.selected_slot_id != previously_selected


async def test_switching_from_booking_to_cancelling(caller) -> None:
    """ "Actually, cancel that" changes task without re-identifying."""
    await _identified_booker(caller)
    await caller.say("any doctor")
    await caller.say("tomorrow")

    await caller.say("actually, I want to cancel my appointment instead")
    session = await caller.session()
    assert session.intent is Intent.CANCEL_APPOINTMENT
    # Identity is preserved across the switch.
    assert session.verification_level is VerificationLevel.IDENTIFIED
    # Booking preferences do not leak into the new task.
    assert session.booking.offered_slot_ids == []


async def test_selecting_by_spoken_time(caller, db, clinic_data) -> None:
    """A caller usually repeats the time back rather than saying "the second"."""
    from app.core.timeutils import speak_time
    from app.database.models.availability import AvailabilitySlot

    await _identified_booker(caller)
    await caller.say("any doctor")
    await caller.say("tomorrow")
    if caller.last.state is ConversationState.PREFERRED_PERIOD:
        await caller.say("morning")
    if caller.last.state is not ConversationState.SELECT_SLOT:
        pytest.skip("only one option was offered, so there is nothing to choose between")

    session = await caller.session()
    wanted_id = session.booking.offered_slot_ids[1]
    wanted = await db.get(AvailabilitySlot, wanted_id)

    await caller.say(speak_time(wanted.start_time, clinic_data.tz))

    session = await caller.session()
    assert session.state is ConversationState.CONFIRM_BOOKING
    assert session.booking.selected_slot_id == wanted_id


async def test_a_correction_marker_alone_does_not_change_state(caller) -> None:
    await _identified_booker(caller)
    await caller.say("Dr Patel")
    await caller.say("next Tuesday")
    session_before = await caller.session()

    await caller.say("actually")
    session_after = await caller.session()
    assert session_after.booking.preferred_date == session_before.booking.preferred_date
    assert session_after.booking.practitioner_id == session_before.booking.practitioner_id
