"""Clinic directory schemas."""

from __future__ import annotations

import uuid

from pydantic import Field

from app.schemas.common import ORMModel


class LocationRead(ORMModel):
    id: uuid.UUID
    name: str
    address: str | None = None
    phone: str | None = None


class ClinicRead(ORMModel):
    id: uuid.UUID
    name: str
    phone: str | None = None
    email: str | None = None
    timezone: str
    locations: list[LocationRead] = Field(default_factory=list)


class PractitionerRead(ORMModel):
    id: uuid.UUID
    display_name: str
    speciality: str | None = None
    accepts_new_patients: bool
    location_id: uuid.UUID | None = None


class AppointmentTypeRead(ORMModel):
    id: uuid.UUID
    slug: str
    name: str
    description: str | None = None
    duration_minutes: int
    new_patient_allowed: bool
    telehealth_allowed: bool
    requires_referral: bool
