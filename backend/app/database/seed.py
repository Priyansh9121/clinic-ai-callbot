"""Development seed data.

===========================================================================
ALL DATA IN THIS MODULE IS FICTIONAL AND FOR DEVELOPMENT ONLY.

"Riverside Medical Clinic", its practitioners and its patients do not exist.
The patient records are synthetic. Never load this into an environment that
holds real patient information, and never replace these values with real
patient data.
===========================================================================

Idempotent: running it twice does not duplicate anything.
"""

from __future__ import annotations

import random
import uuid
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.core.timeutils import utc_now
from app.database.models.appointment_type import AppointmentType
from app.database.models.availability import AvailabilitySlot
from app.database.models.clinic import Clinic, Location
from app.database.models.enums import SlotStatus
from app.database.models.patient import Patient
from app.database.models.practitioner import Practitioner
from app.patients.service import normalise_mobile

logger = get_logger(__name__)

CLINIC_NAME = "Riverside Medical Clinic"
CLINIC_TZ = "Australia/Melbourne"

# Deterministic generation so a re-seed produces the same fixtures and tests
# are reproducible.
SEED = 20260812

PRACTITIONERS = [
    ("Sarah", "Patel", "Dr Sarah Patel", "General Practice", True),
    ("James", "Wilson", "Dr James Wilson", "General Practice", True),
    ("Emily", "Chen", "Dr Emily Chen", "General Practice, Skin Checks", False),
]

APPOINTMENT_TYPES = [
    ("standard_gp", "Standard GP", "A standard consultation.", 15, True, False, False),
    ("long_gp", "Long GP", "A longer consultation for complex issues.", 30, True, False, False),
    ("new_patient", "New Patient", "First appointment for a new patient.", 30, True, False, False),
    ("telehealth", "Telehealth", "Telephone consultation.", 15, False, True, False),
]

# Synthetic patients. Names, dates of birth and numbers are invented; the
# mobile numbers use the 0491 570 xxx range reserved by ACMA for fictitious use.
PATIENTS = [
    ("Margaret", "Hughes", date(1958, 3, 14), "0491570156"),
    ("David", "Nguyen", date(1982, 11, 2), "0491570157"),
    ("Aisha", "Rahman", date(1995, 6, 21), "0491570158"),
    ("Tom", "Baker", date(1974, 1, 30), "0491570159"),
    ("Priya", "Sharma", date(2001, 9, 8), "0491570160"),
]

CLINIC_HOURS: dict[int, tuple[time, time]] = {
    0: (time(8, 0), time(18, 0)),
    1: (time(8, 0), time(18, 0)),
    2: (time(8, 0), time(18, 0)),
    3: (time(8, 0), time(18, 0)),
    4: (time(8, 0), time(18, 0)),
    5: (time(9, 0), time(13, 0)),  # Saturday
}

AVAILABILITY_DAYS = 21


async def seed_all(session: AsyncSession, *, availability_days: int = AVAILABILITY_DAYS) -> Clinic:
    clinic = await _seed_clinic(session)
    locations = await _seed_locations(session, clinic)
    types = await _seed_appointment_types(session, clinic)
    practitioners = await _seed_practitioners(session, clinic, locations[0])
    await _seed_patients(session, clinic)
    await _seed_availability(
        session, clinic, practitioners, locations[0], types, days=availability_days
    )
    await session.commit()

    from app.conversation.vocabulary import clear_cache

    clear_cache()
    logger.info("seed.complete", clinic=clinic.name)
    return clinic


async def _seed_clinic(session: AsyncSession) -> Clinic:
    existing = (
        (await session.execute(select(Clinic).where(Clinic.name == CLINIC_NAME))).scalars().first()
    )
    if existing:
        return existing
    clinic = Clinic(
        name=CLINIC_NAME,
        phone="+61390000000",
        email="reception@riverside-example.invalid",  # .invalid: never routable
        timezone=CLINIC_TZ,
    )
    session.add(clinic)
    await session.flush()
    return clinic


async def _seed_locations(session: AsyncSession, clinic: Clinic) -> list[Location]:
    existing = list(
        (await session.execute(select(Location).where(Location.clinic_id == clinic.id)))
        .scalars()
        .all()
    )
    if existing:
        return existing
    location = Location(
        clinic_id=clinic.id,
        name="Riverside Main",
        address="12 Riverside Drive, Richmond VIC 3121",
        phone="+61390000000",
    )
    session.add(location)
    await session.flush()
    return [location]


async def _seed_appointment_types(session: AsyncSession, clinic: Clinic) -> list[AppointmentType]:
    existing = list(
        (
            await session.execute(
                select(AppointmentType).where(AppointmentType.clinic_id == clinic.id)
            )
        )
        .scalars()
        .all()
    )
    if existing:
        return existing
    created = [
        AppointmentType(
            clinic_id=clinic.id,
            slug=slug,
            name=name,
            description=description,
            duration_minutes=duration,
            new_patient_allowed=new_allowed,
            telehealth_allowed=telehealth,
            requires_referral=referral,
        )
        for slug, name, description, duration, new_allowed, telehealth, referral in (
            APPOINTMENT_TYPES
        )
    ]
    session.add_all(created)
    await session.flush()
    return created


async def _seed_practitioners(
    session: AsyncSession, clinic: Clinic, location: Location
) -> list[Practitioner]:
    existing = list(
        (await session.execute(select(Practitioner).where(Practitioner.clinic_id == clinic.id)))
        .scalars()
        .unique()
        .all()
    )
    if existing:
        return existing
    created = [
        Practitioner(
            clinic_id=clinic.id,
            location_id=location.id,
            first_name=first,
            last_name=last,
            display_name=display,
            speciality=speciality,
            accepts_new_patients=accepts_new,
        )
        for first, last, display, speciality, accepts_new in PRACTITIONERS
    ]
    session.add_all(created)
    await session.flush()
    return created


async def _seed_patients(session: AsyncSession, clinic: Clinic) -> list[Patient]:
    existing = list(
        (await session.execute(select(Patient).where(Patient.clinic_id == clinic.id)))
        .scalars()
        .all()
    )
    if existing:
        return existing
    created = [
        Patient(
            clinic_id=clinic.id,
            first_name=first,
            last_name=last,
            date_of_birth=dob,
            mobile=mobile,
            mobile_normalised=normalise_mobile(mobile),
            email=f"{first.lower()}.{last.lower()}@example.invalid",
        )
        for first, last, dob, mobile in PATIENTS
    ]
    session.add_all(created)
    await session.flush()
    return created


async def _seed_availability(
    session: AsyncSession,
    clinic: Clinic,
    practitioners: list[Practitioner],
    location: Location,
    types: list[AppointmentType],
    *,
    days: int,
) -> None:
    """Generate realistic future availability.

    Each practitioner works clinic hours with a lunch break, offering slots for
    every appointment type at that type's own cadence. A deterministic
    proportion is pre-BOOKED/BLOCKED so searches return a realistic, patchy
    calendar rather than a wall of free time.
    """
    tz = ZoneInfo(clinic.timezone)
    today = utc_now().astimezone(tz).date()

    already = (
        await session.execute(
            select(AvailabilitySlot.id)
            .join(Practitioner, Practitioner.id == AvailabilitySlot.practitioner_id)
            .where(Practitioner.clinic_id == clinic.id)
            .limit(1)
        )
    ).first()
    if already:
        logger.info("seed.availability_exists")
        return

    rng = random.Random(SEED)
    by_slug = {t.slug: t for t in types}
    # A GP clinic runs mostly standard consultations, with dedicated blocks for
    # the longer appointment types. Modelling it that way means a caller asking
    # for a standard appointment with a named doctor on a named day usually
    # finds something, while long/new-patient/telehealth remain genuinely
    # scarcer — which is what exercises the fallback paths.
    special_rotation = ["long_gp", "new_patient", "telehealth"]
    special_block = (time(10, 0), time(11, 30))

    slots: list[AvailabilitySlot] = []
    for day_offset in range(days):
        day = today + timedelta(days=day_offset)
        hours = CLINIC_HOURS.get(day.weekday())
        if hours is None:  # Sunday
            continue
        open_t, close_t = hours

        for index, practitioner in enumerate(practitioners):
            special_type = by_slug[special_rotation[(day_offset + index) % len(special_rotation)]]
            standard_type = by_slug["standard_gp"]

            cursor = datetime.combine(day, open_t, tzinfo=tz)
            end_of_day = datetime.combine(day, close_t, tzinfo=tz)
            lunch_start = datetime.combine(day, time(12, 30), tzinfo=tz)
            lunch_end = datetime.combine(day, time(13, 15), tzinfo=tz)
            special_start = datetime.combine(day, special_block[0], tzinfo=tz)
            special_end = datetime.combine(day, special_block[1], tzinfo=tz)

            while cursor < end_of_day:
                if lunch_start <= cursor < lunch_end:
                    cursor = lunch_end
                    continue
                in_special = special_start <= cursor < special_end
                appointment_type = special_type if in_special else standard_type
                duration = timedelta(minutes=appointment_type.duration_minutes)
                if cursor + duration > end_of_day:
                    break
                # A longer appointment must not run past its block.
                if in_special and cursor + duration > special_end:
                    cursor = special_end
                    continue

                roll = rng.random()
                status = (
                    SlotStatus.BOOKED
                    if roll < 0.35
                    else SlotStatus.BLOCKED
                    if roll < 0.40
                    else SlotStatus.AVAILABLE
                )
                slots.append(
                    AvailabilitySlot(
                        id=uuid.uuid4(),
                        practitioner_id=practitioner.id,
                        location_id=location.id,
                        appointment_type_id=appointment_type.id,
                        start_time=cursor.astimezone(ZoneInfo("UTC")),
                        end_time=(cursor + duration).astimezone(ZoneInfo("UTC")),
                        status=status,
                    )
                )
                cursor += duration

    session.add_all(slots)
    await session.flush()
    available = sum(1 for s in slots if s.status is SlotStatus.AVAILABLE)
    logger.info("seed.availability", total=len(slots), available=available)
