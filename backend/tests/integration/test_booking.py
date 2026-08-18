"""Transactional booking, cancellation and rescheduling."""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from sqlalchemy import func, select

from app.appointments.availability import AvailabilityQuery, AvailabilityService
from app.appointments.errors import AppointmentNotFoundError, SlotNotFoundError
from app.appointments.service import AppointmentService, BookingRequest, StaffAuthorised
from app.core.errors import AppointmentStateError, BookingRuleError, SlotUnavailableError
from app.core.timeutils import DayPeriod, utc_now
from app.database.models.appointment import Appointment
from app.database.models.audit import AuditEvent
from app.database.models.availability import AvailabilitySlot
from app.database.models.enums import AppointmentStatus, AuditAction, BookingSource, SlotStatus

pytestmark = pytest.mark.integration


async def _first_available(db, clinic_data, settings, **kwargs) -> AvailabilitySlot:
    service = AvailabilityService(db, settings)
    slots = await service.search(
        AvailabilityQuery(clinic_id=clinic_data.clinic.id, limit=50, **kwargs), clinic_data.tz
    )
    assert slots, "fixture should provide availability"
    return slots[0]


async def test_booking_commits_and_consumes_the_slot(db, clinic_data, settings) -> None:
    slot = await _first_available(db, clinic_data, settings)
    service = AppointmentService(db, settings)

    result = await service.book(
        BookingRequest(
            clinic_id=clinic_data.clinic.id,
            patient_id=clinic_data.patients["hughes"].id,
            slot_id=slot.id,
            is_new_patient=False,
        )
    )

    assert result.replayed is False
    appointment = result.appointment
    assert appointment.status is AppointmentStatus.CONFIRMED
    assert appointment.start_time == slot.start_time

    # Relationships are loaded, so the caller can speak the result without
    # triggering a lazy load after commit.
    assert appointment.practitioner.display_name

    refreshed = await db.get(AvailabilitySlot, slot.id)
    await db.refresh(refreshed)
    assert refreshed.status is SlotStatus.BOOKED


async def test_booking_is_visible_in_a_separate_session(
    db, sessionmaker_fixture, clinic_data, settings
) -> None:
    """Proves the row was really committed, not just held in the session."""
    slot = await _first_available(db, clinic_data, settings)
    result = await AppointmentService(db, settings).book(
        BookingRequest(
            clinic_id=clinic_data.clinic.id,
            patient_id=clinic_data.patients["hughes"].id,
            slot_id=slot.id,
            is_new_patient=False,
        )
    )

    async with sessionmaker_fixture() as other:
        found = await other.get(Appointment, result.appointment.id)
        assert found is not None
        assert found.status is AppointmentStatus.CONFIRMED


async def test_booking_writes_an_audit_event(db, clinic_data, settings) -> None:
    slot = await _first_available(db, clinic_data, settings)
    result = await AppointmentService(db, settings).book(
        BookingRequest(
            clinic_id=clinic_data.clinic.id,
            patient_id=clinic_data.patients["hughes"].id,
            slot_id=slot.id,
            is_new_patient=False,
        )
    )

    events = (
        (
            await db.execute(
                select(AuditEvent).where(AuditEvent.action == AuditAction.APPOINTMENT_CREATED)
            )
        )
        .scalars()
        .all()
    )
    assert len(events) == 1
    assert events[0].subject_id == result.appointment.id
    # Audit metadata must never carry patient identifiers.
    assert "first_name" not in events[0].metadata_json
    assert "date_of_birth" not in events[0].metadata_json


async def test_double_booking_the_same_slot_is_refused(db, clinic_data, settings) -> None:
    slot = await _first_available(db, clinic_data, settings)
    service = AppointmentService(db, settings)
    await service.book(
        BookingRequest(
            clinic_id=clinic_data.clinic.id,
            patient_id=clinic_data.patients["hughes"].id,
            slot_id=slot.id,
            is_new_patient=False,
        )
    )

    with pytest.raises(SlotUnavailableError):
        await service.book(
            BookingRequest(
                clinic_id=clinic_data.clinic.id,
                patient_id=clinic_data.patients["nguyen"].id,
                slot_id=slot.id,
                is_new_patient=False,
            )
        )

    count = await db.scalar(select(func.count()).select_from(Appointment))
    assert count == 1


async def test_idempotency_key_replays_instead_of_duplicating(db, clinic_data, settings) -> None:
    slot = await _first_available(db, clinic_data, settings)
    service = AppointmentService(db, settings)
    request = BookingRequest(
        clinic_id=clinic_data.clinic.id,
        patient_id=clinic_data.patients["hughes"].id,
        slot_id=slot.id,
        is_new_patient=False,
        idempotency_key="retry-key-0001",
    )

    first = await service.book(request)
    second = await service.book(request)

    assert second.replayed is True
    assert second.appointment.id == first.appointment.id
    assert await db.scalar(select(func.count()).select_from(Appointment)) == 1


async def test_booking_inside_the_lead_time_is_rejected(db, clinic_data, settings) -> None:
    slot = await _first_available(db, clinic_data, settings)
    # Move the slot to 10 minutes from now, inside the 60-minute lead time.
    slot.start_time = utc_now() + timedelta(minutes=10)
    slot.end_time = slot.start_time + timedelta(minutes=15)
    await db.commit()

    with pytest.raises(BookingRuleError):
        await AppointmentService(db, settings).book(
            BookingRequest(
                clinic_id=clinic_data.clinic.id,
                patient_id=clinic_data.patients["hughes"].id,
                slot_id=slot.id,
                is_new_patient=False,
            )
        )
    assert await db.scalar(select(func.count()).select_from(Appointment)) == 0


async def test_new_patient_cannot_book_a_type_that_excludes_them(db, clinic_data, settings) -> None:
    slot = await _first_available(
        db, clinic_data, settings, appointment_type_id=clinic_data.types["telehealth"].id
    )
    with pytest.raises(BookingRuleError):
        await AppointmentService(db, settings).book(
            BookingRequest(
                clinic_id=clinic_data.clinic.id,
                patient_id=clinic_data.patients["hughes"].id,
                slot_id=slot.id,
                is_new_patient=True,
            )
        )


async def test_unknown_slot_is_rejected(db, clinic_data, settings) -> None:
    with pytest.raises(SlotNotFoundError):
        await AppointmentService(db, settings).book(
            BookingRequest(
                clinic_id=clinic_data.clinic.id,
                patient_id=clinic_data.patients["hughes"].id,
                slot_id=uuid.uuid4(),
                is_new_patient=False,
            )
        )


# --- Cancellation -----------------------------------------------------------
async def test_cancellation_frees_a_future_slot(db, clinic_data, settings) -> None:
    slot = await _first_available(db, clinic_data, settings)
    service = AppointmentService(db, settings)
    booked = await service.book(
        BookingRequest(
            clinic_id=clinic_data.clinic.id,
            patient_id=clinic_data.patients["hughes"].id,
            slot_id=slot.id,
            is_new_patient=False,
        )
    )

    cancelled = await service.cancel(
        booked.appointment.id, owner=StaffAuthorised(mechanism="test"), reason="caller request"
    )
    assert cancelled.status is AppointmentStatus.CANCELLED
    assert cancelled.cancelled_at is not None

    freed = await db.get(AvailabilitySlot, slot.id)
    await db.refresh(freed)
    assert freed.status is SlotStatus.AVAILABLE


async def test_a_freed_slot_can_be_rebooked(db, clinic_data, settings) -> None:
    slot = await _first_available(db, clinic_data, settings)
    service = AppointmentService(db, settings)
    first = await service.book(
        BookingRequest(
            clinic_id=clinic_data.clinic.id,
            patient_id=clinic_data.patients["hughes"].id,
            slot_id=slot.id,
            is_new_patient=False,
        )
    )
    await service.cancel(first.appointment.id, owner=StaffAuthorised(mechanism="test"))

    second = await service.book(
        BookingRequest(
            clinic_id=clinic_data.clinic.id,
            patient_id=clinic_data.patients["nguyen"].id,
            slot_id=slot.id,
            is_new_patient=False,
        )
    )
    assert second.appointment.id != first.appointment.id
    assert second.appointment.status is AppointmentStatus.CONFIRMED


async def test_cancelling_twice_is_refused(db, clinic_data, settings) -> None:
    slot = await _first_available(db, clinic_data, settings)
    service = AppointmentService(db, settings)
    booked = await service.book(
        BookingRequest(
            clinic_id=clinic_data.clinic.id,
            patient_id=clinic_data.patients["hughes"].id,
            slot_id=slot.id,
            is_new_patient=False,
        )
    )
    await service.cancel(booked.appointment.id, owner=StaffAuthorised(mechanism="test"))
    with pytest.raises(AppointmentStateError):
        await service.cancel(booked.appointment.id, owner=StaffAuthorised(mechanism="test"))


async def test_cannot_cancel_another_patients_appointment(db, clinic_data, settings) -> None:
    """Authorisation, not filtering: a known id is still refused."""
    slot = await _first_available(db, clinic_data, settings)
    service = AppointmentService(db, settings)
    booked = await service.book(
        BookingRequest(
            clinic_id=clinic_data.clinic.id,
            patient_id=clinic_data.patients["hughes"].id,
            slot_id=slot.id,
            is_new_patient=False,
        )
    )

    # Capture the id first: a failed operation rolls back, which expires every
    # ORM object in the session.
    appointment_id = booked.appointment.id

    with pytest.raises(AppointmentNotFoundError):
        await service.cancel(appointment_id, owner=clinic_data.patients["nguyen"].id)

    still_confirmed = await db.get(Appointment, appointment_id)
    await db.refresh(still_confirmed)
    assert still_confirmed.status is AppointmentStatus.CONFIRMED


# --- Rescheduling -----------------------------------------------------------
async def test_reschedule_moves_the_appointment_atomically(db, clinic_data, settings) -> None:
    availability = AvailabilityService(db, settings)
    slots = await availability.search(
        AvailabilityQuery(clinic_id=clinic_data.clinic.id, limit=50), clinic_data.tz
    )
    original_slot, new_slot = slots[0], slots[10]
    service = AppointmentService(db, settings)

    booked = await service.book(
        BookingRequest(
            clinic_id=clinic_data.clinic.id,
            patient_id=clinic_data.patients["hughes"].id,
            slot_id=original_slot.id,
            is_new_patient=False,
        )
    )
    moved = await service.reschedule(
        booked.appointment.id, new_slot.id, owner=StaffAuthorised(mechanism="test")
    )

    assert moved.id != booked.appointment.id
    assert moved.start_time == new_slot.start_time
    assert moved.status is AppointmentStatus.CONFIRMED

    old = await db.get(Appointment, booked.appointment.id)
    await db.refresh(old)
    assert old.status is AppointmentStatus.CANCELLED
    assert old.rescheduled_to_id == moved.id

    freed = await db.get(AvailabilitySlot, original_slot.id)
    taken = await db.get(AvailabilitySlot, new_slot.id)
    await db.refresh(freed)
    await db.refresh(taken)
    assert freed.status is SlotStatus.AVAILABLE
    assert taken.status is SlotStatus.BOOKED


async def test_reschedule_onto_a_taken_slot_leaves_the_original_intact(
    db, clinic_data, settings
) -> None:
    """The caller must not lose their appointment because the new slot went."""
    availability = AvailabilityService(db, settings)
    slots = await availability.search(
        AvailabilityQuery(clinic_id=clinic_data.clinic.id, limit=50), clinic_data.tz
    )
    original_slot, contested = slots[0], slots[10]
    service = AppointmentService(db, settings)

    booked = await service.book(
        BookingRequest(
            clinic_id=clinic_data.clinic.id,
            patient_id=clinic_data.patients["hughes"].id,
            slot_id=original_slot.id,
            is_new_patient=False,
        )
    )
    await service.book(
        BookingRequest(
            clinic_id=clinic_data.clinic.id,
            patient_id=clinic_data.patients["nguyen"].id,
            slot_id=contested.id,
            is_new_patient=False,
        )
    )
    appointment_id = booked.appointment.id
    original_slot_id = original_slot.id

    with pytest.raises(SlotUnavailableError):
        await service.reschedule(
            appointment_id, contested.id, owner=StaffAuthorised(mechanism="test")
        )

    unchanged = await db.get(Appointment, appointment_id)
    await db.refresh(unchanged)
    assert unchanged.status is AppointmentStatus.CONFIRMED
    assert unchanged.availability_slot_id == original_slot_id


async def test_reschedule_within_the_same_day_is_allowed(db, clinic_data, settings) -> None:
    """Regression: the partial unique index on (patient, start) must not fire
    when an appointment moves between two times on the same day."""
    availability = AvailabilityService(db, settings)
    tomorrow = (utc_now().astimezone(clinic_data.tz) + timedelta(days=1)).date()
    slots = await availability.search(
        AvailabilityQuery(
            clinic_id=clinic_data.clinic.id,
            practitioner_id=clinic_data.practitioners["patel"].id,
            day=tomorrow,
            period=DayPeriod.ANY,
            limit=50,
        ),
        clinic_data.tz,
    )
    if len(slots) < 2:
        pytest.skip("fixture did not produce two same-day slots")

    service = AppointmentService(db, settings)
    booked = await service.book(
        BookingRequest(
            clinic_id=clinic_data.clinic.id,
            patient_id=clinic_data.patients["hughes"].id,
            slot_id=slots[0].id,
            is_new_patient=False,
        )
    )
    moved = await service.reschedule(
        booked.appointment.id, slots[1].id, owner=StaffAuthorised(mechanism="test")
    )
    assert moved.status is AppointmentStatus.CONFIRMED


async def test_upcoming_excludes_cancelled_appointments(db, clinic_data, settings) -> None:
    slot = await _first_available(db, clinic_data, settings)
    service = AppointmentService(db, settings)
    booked = await service.book(
        BookingRequest(
            clinic_id=clinic_data.clinic.id,
            patient_id=clinic_data.patients["hughes"].id,
            slot_id=slot.id,
            is_new_patient=False,
            source=BookingSource.RECEPTION,
        )
    )
    assert len(await service.upcoming_for_patient(clinic_data.patients["hughes"].id)) == 1

    await service.cancel(booked.appointment.id, owner=StaffAuthorised(mechanism="test"))
    assert await service.upcoming_for_patient(clinic_data.patients["hughes"].id) == []


# --- "after 3" is a lower bound, not a period ------------------------------
async def test_not_before_narrows_the_window_below_a_period(db, clinic_data, settings) -> None:
    """A period alone is too coarse for "after 3".

    AFTERNOON starts at midday, so a caller who asked for something after 3
    was offered 12:15. `not_before` was parsed from the utterance and stored on
    the booking draft, and even counted as a preference change — it simply
    never reached AvailabilityQuery, so the filter it describes did not exist.
    """
    from datetime import time

    service = AvailabilityService(db, settings)
    day = (utc_now().astimezone(clinic_data.tz) + timedelta(days=3)).date()

    afternoon = await service.search(
        AvailabilityQuery(clinic_id=clinic_data.clinic.id, day=day, period=DayPeriod.AFTERNOON),
        clinic_data.tz,
    )
    after_three = await service.search(
        AvailabilityQuery(
            clinic_id=clinic_data.clinic.id,
            day=day,
            period=DayPeriod.AFTERNOON,
            not_before=time(15, 0),
        ),
        clinic_data.tz,
    )

    assert afternoon, "fixture should provide afternoon availability to narrow"
    for slot in after_three:
        local = slot.start_time.astimezone(clinic_data.tz)
        assert local.hour >= 15, f"offered {local:%H:%M} for a not_before of 15:00"
    assert len(after_three) < len(afternoon), (
        "not_before did not narrow the window at all — the filter is not applied"
    )


async def test_not_before_applies_without_a_period(db, clinic_data, settings) -> None:
    """ "After 3" with no period stated must still bound the day."""
    from datetime import time

    service = AvailabilityService(db, settings)
    day = (utc_now().astimezone(clinic_data.tz) + timedelta(days=3)).date()

    slots = await service.search(
        AvailabilityQuery(
            clinic_id=clinic_data.clinic.id,
            day=day,
            period=DayPeriod.ANY,
            not_before=time(15, 0),
        ),
        clinic_data.tz,
    )

    assert slots, "fixture should provide late-afternoon availability"
    for slot in slots:
        assert slot.start_time.astimezone(clinic_data.tz).hour >= 15


# --- Rollback must not reach beyond the mutation that failed ----------------
async def _provisional(db, clinic_data, surname: str):
    from datetime import date as _date

    from app.patients.service import PatientService

    return await PatientService(db).create_provisional(
        clinic_id=clinic_data.clinic.id,
        first_name="Rollback",
        last_name=surname,
        date_of_birth=_date(1980, 1, 1),
        mobile=None,
    )


async def test_a_failed_booking_does_not_delete_the_caller(db, clinic_data, settings) -> None:
    """The caller is created, then the booking fails, and the caller vanished.

    A first-time caller gets a provisional patient row part-way through the
    call, on the same session everything else uses. `book()` rolled that
    session back on any failure, which undid the patient too — so the next
    turn could not find the record it had just created, and the caller was
    asked for their details again or transferred.

    The undo must reach only as far as the mutation that failed.
    """
    from app.patients.service import PatientService

    patient = await _provisional(db, clinic_data, "Booking")
    patient_id = patient.id
    service = AppointmentService(db, settings)

    with pytest.raises(SlotNotFoundError):
        await service.book(
            BookingRequest(
                clinic_id=clinic_data.clinic.id,
                patient_id=patient_id,
                slot_id=uuid.uuid4(),  # fails after the patient exists
                source=BookingSource.AI_CALLBOT,
                is_new_patient=True,
            )
        )

    assert await PatientService(db).get(patient_id) is not None, (
        "an unrelated booking failure destroyed the provisional patient"
    )


async def test_a_failed_cancellation_does_not_delete_the_caller(db, clinic_data, settings) -> None:
    from app.patients.service import PatientService

    patient = await _provisional(db, clinic_data, "Cancel")
    patient_id = patient.id
    service = AppointmentService(db, settings)

    with pytest.raises(AppointmentNotFoundError):
        await service.cancel(uuid.uuid4(), owner=StaffAuthorised(mechanism="test"))

    assert await PatientService(db).get(patient_id) is not None


async def test_a_failed_reschedule_does_not_delete_the_caller(db, clinic_data, settings) -> None:
    from app.patients.service import PatientService

    patient = await _provisional(db, clinic_data, "Reschedule")
    patient_id = patient.id
    service = AppointmentService(db, settings)

    with pytest.raises(AppointmentNotFoundError):
        await service.reschedule(
            uuid.uuid4(), uuid.uuid4(), owner=StaffAuthorised(mechanism="test")
        )

    assert await PatientService(db).get(patient_id) is not None


async def test_a_failed_booking_leaves_no_half_written_appointment(
    db, clinic_data, settings
) -> None:
    """The other half of the same boundary: the failure must undo its own work.

    Scoping the rollback to a savepoint must not turn a failure into a
    partially-applied booking — the slot stays available and no appointment row
    survives.
    """
    slot = await _first_available(db, clinic_data, settings)
    patient = await _provisional(db, clinic_data, "Partial")
    service = AppointmentService(db, settings)
    before = (await db.execute(select(func.count()).select_from(Appointment))).scalar_one()

    # Inside the booking lead time, so _enforce_booking_rules rejects it after
    # the slot has already been locked and read.
    slot.start_time = utc_now() + timedelta(minutes=1)
    await db.flush()

    with pytest.raises(BookingRuleError):
        await service.book(
            BookingRequest(
                clinic_id=clinic_data.clinic.id,
                patient_id=patient.id,
                slot_id=slot.id,
                is_new_patient=False,
                source=BookingSource.AI_CALLBOT,
            )
        )

    after = (await db.execute(select(func.count()).select_from(Appointment))).scalar_one()
    assert after == before, "a failed booking left an appointment row behind"
    refreshed = await db.get(AvailabilitySlot, slot.id)
    assert refreshed.status is SlotStatus.AVAILABLE, "a failed booking consumed the slot"
