"""Booking concurrency.

Two callers reaching the same slot at the same instant is the failure mode that
matters most: it is rare in testing and routine in production. Each test uses
genuinely separate sessions and connections, so the database — not the ORM
identity map — is what resolves the race.
"""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import func, select

from app.appointments.availability import AvailabilityQuery, AvailabilityService
from app.appointments.service import AppointmentService, BookingRequest
from app.core.errors import ConflictError, DuplicateBookingError, SlotUnavailableError
from app.database.models.appointment import Appointment
from app.database.models.availability import AvailabilitySlot
from app.database.models.enums import AppointmentStatus, SlotStatus

pytestmark = pytest.mark.integration


async def _book_in_new_session(sessionmaker, settings, request: BookingRequest):
    """Book on a fresh session/connection, returning the result or the error."""
    async with sessionmaker() as session:
        service = AppointmentService(session, settings)
        try:
            return await service.book(request)
        except Exception as exc:  # returned so the caller can classify it
            return exc


async def test_two_callers_racing_for_one_slot_produce_exactly_one_booking(
    db, sessionmaker_fixture, clinic_data, settings
) -> None:
    slots = await AvailabilityService(db, settings).search(
        AvailabilityQuery(clinic_id=clinic_data.clinic.id, limit=1), clinic_data.tz
    )
    slot_id = slots[0].id

    results = await asyncio.gather(
        _book_in_new_session(
            sessionmaker_fixture,
            settings,
            BookingRequest(
                clinic_id=clinic_data.clinic.id,
                patient_id=clinic_data.patients["hughes"].id,
                slot_id=slot_id,
                is_new_patient=False,
            ),
        ),
        _book_in_new_session(
            sessionmaker_fixture,
            settings,
            BookingRequest(
                clinic_id=clinic_data.clinic.id,
                patient_id=clinic_data.patients["nguyen"].id,
                slot_id=slot_id,
                is_new_patient=False,
            ),
        ),
    )

    successes = [r for r in results if not isinstance(r, Exception)]
    failures = [r for r in results if isinstance(r, Exception)]

    assert len(successes) == 1, f"expected exactly one winner, got {results}"
    assert len(failures) == 1
    # The loser gets a clean domain error, never a raw database exception.
    assert isinstance(failures[0], SlotUnavailableError | DuplicateBookingError)

    async with sessionmaker_fixture() as session:
        confirmed = await session.scalar(
            select(func.count())
            .select_from(Appointment)
            .where(
                Appointment.availability_slot_id == slot_id,
                Appointment.status == AppointmentStatus.CONFIRMED,
            )
        )
        assert confirmed == 1
        slot = await session.get(AvailabilitySlot, slot_id)
        assert slot.status is SlotStatus.BOOKED


@pytest.mark.parametrize("contenders", [5, 10])
async def test_many_callers_racing_for_one_slot(
    db, sessionmaker_fixture, clinic_data, settings, contenders: int
) -> None:
    slots = await AvailabilityService(db, settings).search(
        AvailabilityQuery(clinic_id=clinic_data.clinic.id, limit=1), clinic_data.tz
    )
    slot_id = slots[0].id
    patients = [clinic_data.patients["hughes"].id, clinic_data.patients["nguyen"].id]

    results = await asyncio.gather(
        *(
            _book_in_new_session(
                sessionmaker_fixture,
                settings,
                BookingRequest(
                    clinic_id=clinic_data.clinic.id,
                    patient_id=patients[i % len(patients)],
                    slot_id=slot_id,
                    is_new_patient=False,
                ),
            )
            for i in range(contenders)
        )
    )

    successes = [r for r in results if not isinstance(r, Exception)]
    assert len(successes) == 1
    assert all(isinstance(r, ConflictError) for r in results if isinstance(r, Exception))

    async with sessionmaker_fixture() as session:
        confirmed = await session.scalar(
            select(func.count())
            .select_from(Appointment)
            .where(Appointment.status == AppointmentStatus.CONFIRMED)
        )
        assert confirmed == 1


async def test_concurrent_retries_with_one_idempotency_key_book_once(
    db, sessionmaker_fixture, clinic_data, settings
) -> None:
    """A client retrying a timed-out booking must not create a second one."""
    slots = await AvailabilityService(db, settings).search(
        AvailabilityQuery(clinic_id=clinic_data.clinic.id, limit=1), clinic_data.tz
    )
    request = BookingRequest(
        clinic_id=clinic_data.clinic.id,
        patient_id=clinic_data.patients["hughes"].id,
        slot_id=slots[0].id,
        is_new_patient=False,
        idempotency_key="concurrent-retry-key",
    )

    results = await asyncio.gather(
        *(_book_in_new_session(sessionmaker_fixture, settings, request) for _ in range(4))
    )
    successes = [r for r in results if not isinstance(r, Exception)]
    assert successes, "at least one attempt must succeed"

    async with sessionmaker_fixture() as session:
        total = await session.scalar(select(func.count()).select_from(Appointment))
        assert total == 1

    # Every successful result refers to the same appointment.
    assert len({r.appointment.id for r in successes}) == 1


async def test_different_slots_book_concurrently_without_interference(
    db, sessionmaker_fixture, clinic_data, settings
) -> None:
    """The lock must be per-slot, not a global bottleneck."""
    slots = await AvailabilityService(db, settings).search(
        AvailabilityQuery(clinic_id=clinic_data.clinic.id, limit=6), clinic_data.tz
    )
    # Distinct start times, so the (patient, start_time) index cannot collide.
    chosen = []
    seen_starts = set()
    for slot in slots:
        if slot.start_time not in seen_starts:
            chosen.append(slot)
            seen_starts.add(slot.start_time)
        if len(chosen) == 3:
            break

    results = await asyncio.gather(
        *(
            _book_in_new_session(
                sessionmaker_fixture,
                settings,
                BookingRequest(
                    clinic_id=clinic_data.clinic.id,
                    patient_id=clinic_data.patients["hughes"].id,
                    slot_id=slot.id,
                    is_new_patient=False,
                ),
            )
            for slot in chosen
        )
    )
    assert all(not isinstance(r, Exception) for r in results), results

    async with sessionmaker_fixture() as session:
        total = await session.scalar(select(func.count()).select_from(Appointment))
        assert total == len(chosen)


async def test_a_patient_cannot_be_double_booked_at_the_same_time(
    db, sessionmaker_fixture, clinic_data, settings
) -> None:
    """Two practitioners, same instant, same patient — the index must refuse."""
    availability = AvailabilityService(db, settings)
    patel = await availability.search(
        AvailabilityQuery(
            clinic_id=clinic_data.clinic.id,
            practitioner_id=clinic_data.practitioners["patel"].id,
            limit=1,
        ),
        clinic_data.tz,
    )
    wilson_slots = await availability.search(
        AvailabilityQuery(
            clinic_id=clinic_data.clinic.id,
            practitioner_id=clinic_data.practitioners["wilson"].id,
            limit=30,
        ),
        clinic_data.tz,
    )
    same_time = next((s for s in wilson_slots if s.start_time == patel[0].start_time), None)
    assert same_time is not None, "fixture should have overlapping practitioner slots"

    results = await asyncio.gather(
        _book_in_new_session(
            sessionmaker_fixture,
            settings,
            BookingRequest(
                clinic_id=clinic_data.clinic.id,
                patient_id=clinic_data.patients["hughes"].id,
                slot_id=patel[0].id,
                is_new_patient=False,
            ),
        ),
        _book_in_new_session(
            sessionmaker_fixture,
            settings,
            BookingRequest(
                clinic_id=clinic_data.clinic.id,
                patient_id=clinic_data.patients["hughes"].id,
                slot_id=same_time.id,
                is_new_patient=False,
            ),
        ),
    )
    successes = [r for r in results if not isinstance(r, Exception)]
    assert len(successes) == 1


# --- What the losing caller actually hears ----------------------------------
async def test_the_caller_who_loses_the_race_is_offered_alternatives(
    caller, db, clinic_data, settings
) -> None:
    """The DB race is settled correctly; this is about the sentence that follows.

    Slots are offered optimistically and only locked at confirmation, so a slot
    can be taken between being read out and being accepted. That window is the
    reason SlotStatus.HELD exists in the schema — and the reason it is not
    used: the race is already resolved correctly by SELECT FOR UPDATE, so the
    only thing left to get right is the caller's experience of losing it.

    A technical failure here would be a bad ending to a call that can still
    succeed.
    """
    from app.appointments.service import AppointmentService, BookingRequest
    from app.conversation.states import ConversationState
    from app.database.models.enums import BookingSource, SlotStatus

    await caller.say("I'd like to book an appointment")
    await caller.say("existing patient")
    await caller.say("Hughes")
    await caller.say("14 March 1958")
    await caller.say("standard")
    await caller.say("any doctor")
    await caller.say("tomorrow")
    if caller.last.state is ConversationState.PREFERRED_PERIOD:
        await caller.say("any time")
    if caller.last.state is ConversationState.SELECT_SLOT:
        await caller.say("the first one")
    assert caller.last.state is ConversationState.CONFIRM_BOOKING, caller.dump()

    session = await caller.session()
    contested = session.booking.selected_slot_id
    assert contested is not None

    # Another caller takes it while this one is deciding.
    other = clinic_data.patients["nguyen"]
    await AppointmentService(db, settings).book(
        BookingRequest(
            clinic_id=clinic_data.clinic.id,
            patient_id=other.id,
            slot_id=contested,
            is_new_patient=False,
            source=BookingSource.RECEPTION,
        )
    )
    taken = await db.get(AvailabilitySlot, contested)
    assert taken.status is SlotStatus.BOOKED

    await caller.say("yes please")

    reply = caller.last.reply.lower()
    assert "sorry" in reply or "taken" in reply or "no longer" in reply, (
        f"the caller was not told the time had gone: {caller.last.reply!r}"
    )
    assert caller.last.state is not ConversationState.HUMAN_TRANSFER, (
        "losing a slot race dropped the caller to reception instead of re-offering"
    )
    after = await caller.session()
    assert after.booking.selected_slot_id != contested
