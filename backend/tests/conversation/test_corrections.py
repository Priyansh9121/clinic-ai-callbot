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


async def _offered_slots(caller, db):
    """Drive a booking to SELECT_SLOT and return the slots actually offered."""
    from app.database.models.availability import AvailabilitySlot

    await _identified_booker(caller)
    await caller.say("any doctor")
    await caller.say("tomorrow")
    if caller.last.state is ConversationState.PREFERRED_PERIOD:
        await caller.say("any time")

    session = await caller.session()
    assert session.state is ConversationState.SELECT_SLOT, (
        f"expected options to be offered, got {session.state}"
    )
    return [await db.get(AvailabilitySlot, sid) for sid in session.booking.offered_slot_ids]


async def test_repeating_an_offered_time_selects_it_instead_of_searching_again(
    caller, db, clinic_data
) -> None:
    """The commonest way a caller picks a slot, and it used to be unanswerable.

    A bare clock time is two things at once: a choice between the options just
    read out, and — since 12 pm is an afternoon — a period preference. Read as
    a preference it counted as a correction, which discarded the offers and
    re-ran the search, so the caller was offered the same slot again and could
    never get past this state however many times they repeated themselves.

    Deliberately picks the LAST slot offered: it is the one most likely to sit
    in a different period from the caller's current preference, which is what
    made the old behaviour intermittent and date-dependent rather than obvious.
    """
    from app.core.timeutils import speak_time

    offered = await _offered_slots(caller, db)
    wanted = offered[-1]

    await caller.say(speak_time(wanted.start_time, clinic_data.tz))

    session = await caller.session()
    assert session.state is ConversationState.CONFIRM_BOOKING, (
        "repeating an offered time was treated as a new search, not a choice"
    )
    assert session.booking.selected_slot_id == wanted.id


async def test_a_time_that_was_not_offered_is_still_a_correction(
    caller, db, clinic_data
) -> None:
    """The other half: the fix must not swallow a genuine change of mind.

    "Make it 7 am" when no 7 am was offered is a caller changing what they
    want, and must still clear the options and search again rather than being
    mistaken for a selection.
    """
    offered = await _offered_slots(caller, db)
    offered_hours = {s.start_time.astimezone(clinic_data.tz).hour for s in offered}
    unoffered = next(h for h in range(6, 23) if h not in offered_hours)
    spoken = f"{unoffered % 12 or 12} {'am' if unoffered < 12 else 'pm'}"

    await caller.say(spoken)

    session = await caller.session()
    assert session.booking.selected_slot_id is None, (
        f"{spoken!r} was not among the options, so it must not select one"
    )
    assert session.state is not ConversationState.CONFIRM_BOOKING, (
        "a time that was never offered must not reach confirmation"
    )


async def test_a_correction_marker_alone_does_not_change_state(caller) -> None:
    await _identified_booker(caller)
    await caller.say("Dr Patel")
    await caller.say("next Tuesday")
    session_before = await caller.session()

    await caller.say("actually")
    session_after = await caller.session()
    assert session_after.booking.preferred_date == session_before.booking.preferred_date
    assert session_after.booking.practitioner_id == session_before.booking.practitioner_id
