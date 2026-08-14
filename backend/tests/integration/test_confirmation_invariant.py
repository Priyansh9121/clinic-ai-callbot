"""No voice path may change a booking before the caller explicitly agrees.

The invariant is enforced in the conversation state machine, not in
AppointmentService — deliberately. A `confirmed=True` argument on the service
would be a flag any caller could pass, proving nothing; the real control is
that the three mutating calls are each reachable only from their own
confirmation state and only on an affirmative.

The other way in is the staff API, which is a different trust boundary:
`require_role(Role.STAFF)` and a human who is not the caller.

These tests drive the actual conversation and then check the database, because
the interesting failure is a mutation happening after something that is not a
yes.
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select

from app.conversation.states import ConversationState
from app.database.models.appointment import Appointment
from app.database.models.enums import AppointmentStatus

pytestmark = pytest.mark.integration


async def _identified(caller):
    await caller.say("I'd like to book an appointment")
    await caller.say("existing patient")
    await caller.say("Hughes")
    await caller.say("14 March 1958")
    await caller.say("standard")
    return caller


async def _at_confirmation(caller):
    await _identified(caller)
    await caller.say("any doctor")
    await caller.say("tomorrow")
    if caller.last.state is ConversationState.PREFERRED_PERIOD:
        await caller.say("any time")
    if caller.last.state is ConversationState.SELECT_SLOT:
        await caller.say("the first one")
    assert caller.last.state is ConversationState.CONFIRM_BOOKING, caller.dump()
    return caller


async def _appointments(db) -> int:
    return (await db.execute(select(func.count()).select_from(Appointment))).scalar_one()


async def test_reaching_the_prompt_books_nothing(caller, db) -> None:
    """Everything up to the confirmation must be read-only."""
    before = await _appointments(db)
    await _at_confirmation(caller)

    assert await _appointments(db) == before, "an appointment was created before any yes"


@pytest.mark.parametrize(
    "reply",
    [
        "no thanks",  # explicit decline
        "hmm",  # ambiguous
        "actually make it next Monday",  # correction
        "what are your opening hours",  # a different question entirely
    ],
)
async def test_only_an_affirmative_creates_the_booking(caller, db, reply: str) -> None:
    before = await _appointments(db)
    await _at_confirmation(caller)

    await caller.say(reply)

    assert await _appointments(db) == before, f"{reply!r} was treated as consent"


async def test_yes_at_the_prompt_does_create_it(caller, db) -> None:
    """The invariant must not be satisfied by never booking anything."""
    before = await _appointments(db)
    await _at_confirmation(caller)

    await caller.say("yes please")

    assert await _appointments(db) == before + 1, caller.dump()


async def test_a_cancellation_needs_an_explicit_yes(caller, db, clinic_data) -> None:
    """Reaching the cancel prompt must not itself cancel anything."""
    await _at_confirmation(caller)
    await caller.say("yes please")
    booked = (
        await db.execute(select(Appointment).order_by(Appointment.created_at.desc()).limit(1))
    ).scalar_one()
    assert booked.status is AppointmentStatus.CONFIRMED

    await caller.say("actually I need to cancel my appointment")
    if caller.last.state is ConversationState.CANCEL_LOOKUP:
        await caller.say("the first one")

    await db.refresh(booked)
    assert booked.status is AppointmentStatus.CONFIRMED, (
        "the appointment was cancelled before the caller confirmed"
    )

    await caller.say("no, leave it")
    await db.refresh(booked)
    assert booked.status is AppointmentStatus.CONFIRMED, "a decline cancelled the appointment"
