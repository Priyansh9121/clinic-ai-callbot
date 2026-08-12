"""Read-only clinic directory queries."""

from __future__ import annotations

import uuid
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFoundError
from app.database.models.appointment_type import AppointmentType
from app.database.models.clinic import Clinic, Location
from app.database.models.practitioner import Practitioner


class ClinicService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_default_clinic(self) -> Clinic:
        """The single active clinic this deployment serves.

        Multi-tenancy is out of scope for now; resolving the clinic in one
        place means adding it later (by phone number or SIP trunk) touches
        exactly this method.
        """
        result = await self._session.execute(
            select(Clinic).where(Clinic.active.is_(True)).order_by(Clinic.created_at).limit(1)
        )
        clinic = result.scalars().first()
        if clinic is None:
            raise NotFoundError(
                detail="no active clinic configured",
                public_message="This service is not configured yet.",
            )
        return clinic

    async def get_clinic(self, clinic_id: uuid.UUID) -> Clinic:
        clinic = await self._session.get(Clinic, clinic_id)
        if clinic is None or not clinic.active:
            raise NotFoundError(detail="clinic not found")
        return clinic

    @staticmethod
    def timezone_of(clinic: Clinic) -> ZoneInfo:
        return ZoneInfo(clinic.timezone)

    async def list_locations(self, clinic_id: uuid.UUID) -> list[Location]:
        result = await self._session.execute(
            select(Location)
            .where(Location.clinic_id == clinic_id, Location.active.is_(True))
            .order_by(Location.name)
        )
        return list(result.scalars().all())

    async def list_practitioners(
        self, clinic_id: uuid.UUID, *, accepting_new_patients: bool | None = None
    ) -> list[Practitioner]:
        stmt = (
            select(Practitioner)
            .where(Practitioner.clinic_id == clinic_id, Practitioner.active.is_(True))
            .order_by(Practitioner.last_name, Practitioner.first_name)
        )
        if accepting_new_patients is not None:
            stmt = stmt.where(Practitioner.accepts_new_patients.is_(accepting_new_patients))
        result = await self._session.execute(stmt)
        return list(result.scalars().unique().all())

    async def get_practitioner(self, practitioner_id: uuid.UUID) -> Practitioner:
        practitioner = await self._session.get(Practitioner, practitioner_id)
        if practitioner is None or not practitioner.active:
            raise NotFoundError(detail="practitioner not found")
        return practitioner

    async def list_appointment_types(
        self, clinic_id: uuid.UUID, *, new_patient_only: bool = False
    ) -> list[AppointmentType]:
        stmt = (
            select(AppointmentType)
            .where(AppointmentType.clinic_id == clinic_id, AppointmentType.active.is_(True))
            .order_by(AppointmentType.duration_minutes)
        )
        if new_patient_only:
            stmt = stmt.where(AppointmentType.new_patient_allowed.is_(True))
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def get_appointment_type(self, appointment_type_id: uuid.UUID) -> AppointmentType:
        appointment_type = await self._session.get(AppointmentType, appointment_type_id)
        if appointment_type is None or not appointment_type.active:
            raise NotFoundError(detail="appointment type not found")
        return appointment_type

    async def get_appointment_type_by_slug(
        self, clinic_id: uuid.UUID, slug: str
    ) -> AppointmentType | None:
        result = await self._session.execute(
            select(AppointmentType).where(
                AppointmentType.clinic_id == clinic_id,
                AppointmentType.slug == slug,
                AppointmentType.active.is_(True),
            )
        )
        return result.scalars().first()
