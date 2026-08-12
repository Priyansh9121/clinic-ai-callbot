"""Clinic vocabulary for entity extraction.

Names the caller might say are matched against real database records. That is
the guard against a hallucinated practitioner: if "Dr Smith" is not in this
clinic's vocabulary, no id is produced and the flow asks again rather than
booking with someone who does not work here.

Cached briefly because it is read on every turn and changes rarely.
"""

from __future__ import annotations

import time
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.conversation.nlu.baseline import (
    AppointmentTypeTerm,
    PractitionerTerm,
    Vocabulary,
)
from app.database.models.appointment_type import AppointmentType
from app.database.models.practitioner import Practitioner

CACHE_TTL_SECONDS = 60.0
_cache: dict[uuid.UUID, tuple[float, Vocabulary]] = {}

#: Spoken synonyms for appointment types, keyed by slug. Kept beside the seed
#: data rather than in the database because they are language, not clinic
#: configuration.
_TYPE_ALIASES: dict[str, tuple[str, ...]] = {
    "standard_gp": (
        "standard",
        "standard appointment",
        "normal appointment",
        "regular",
        "quick",
        "short appointment",
        "usual",
    ),
    "long_gp": (
        "long",
        "long appointment",
        "longer appointment",
        "double",
        "extended",
        "long consult",
        "more time",
    ),
    "new_patient": ("new patient", "first appointment", "first visit", "registration"),
    "telehealth": (
        "telehealth",
        "phone appointment",
        "phone consult",
        "over the phone",
        "video",
        "remote",
        "telephone appointment",
    ),
}


def _practitioner_aliases(practitioner: Practitioner) -> tuple[str, ...]:
    """Surface forms a caller might use, longest-first at match time.

    Includes the bare surname because callers say "Patel" far more often than
    "Doctor Sarah Patel".
    """
    first = practitioner.first_name.lower()
    last = practitioner.last_name.lower()
    display = practitioner.display_name.lower().replace(".", "")
    aliases = {
        last,
        f"dr {last}",
        f"doctor {last}",
        f"{first} {last}",
        f"dr {first} {last}",
        f"doctor {first} {last}",
        display,
        display.replace("dr ", "doctor "),
    }
    return tuple(sorted(aliases, key=len, reverse=True))


def _type_aliases(appointment_type: AppointmentType) -> tuple[str, ...]:
    aliases = {appointment_type.name.lower(), *(_TYPE_ALIASES.get(appointment_type.slug, ()))}
    return tuple(sorted(aliases, key=len, reverse=True))


async def load_vocabulary(session: AsyncSession, clinic_id: uuid.UUID) -> Vocabulary:
    cached = _cache.get(clinic_id)
    if cached is not None and cached[0] > time.monotonic():
        return cached[1]

    practitioners = (
        (
            await session.execute(
                select(Practitioner).where(
                    Practitioner.clinic_id == clinic_id, Practitioner.active.is_(True)
                )
            )
        )
        .scalars()
        .unique()
        .all()
    )
    appointment_types = (
        (
            await session.execute(
                select(AppointmentType).where(
                    AppointmentType.clinic_id == clinic_id, AppointmentType.active.is_(True)
                )
            )
        )
        .scalars()
        .all()
    )

    vocabulary = Vocabulary(
        practitioners=tuple(
            PractitionerTerm(
                practitioner_id=p.id,
                display_name=p.display_name,
                aliases=_practitioner_aliases(p),
            )
            for p in practitioners
        ),
        appointment_types=tuple(
            AppointmentTypeTerm(
                appointment_type_id=t.id,
                slug=t.slug,
                name=t.name,
                aliases=_type_aliases(t),
            )
            for t in appointment_types
        ),
    )
    _cache[clinic_id] = (time.monotonic() + CACHE_TTL_SECONDS, vocabulary)
    return vocabulary


def clear_cache() -> None:
    """Invalidate after seeding or an admin edit (and between tests)."""
    _cache.clear()
