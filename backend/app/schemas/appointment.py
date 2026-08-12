"""Availability and appointment schemas.

Patient identity is deliberately thin in responses: a first name and nothing
else. Enough for reception to recognise a booking, not enough for an API
consumer to build a patient list.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime

from pydantic import BaseModel, Field, field_validator, model_validator

from app.core.timeutils import DayPeriod
from app.database.models.enums import AppointmentStatus, BookingSource
from app.schemas.common import ORMModel


class SlotRead(ORMModel):
    id: uuid.UUID
    start_time: datetime
    end_time: datetime
    practitioner_id: uuid.UUID
    location_id: uuid.UUID
    appointment_type_id: uuid.UUID


class AvailabilityQueryParams(BaseModel):
    """Validated query parameters for ``GET /api/availability``."""

    appointment_type_id: uuid.UUID | None = None
    practitioner_id: uuid.UUID | None = None
    location_id: uuid.UUID | None = None
    day: date | None = None
    period: DayPeriod = DayPeriod.ANY
    limit: int = Field(default=20, ge=1, le=100)

    @field_validator("day")
    @classmethod
    def _not_in_the_past(cls, value: date | None) -> date | None:
        # Cheap sanity bound; the service applies the authoritative clinic-local
        # lead-time check.
        if value is not None and value < date(2000, 1, 1):
            raise ValueError("day is out of range")
        return value


class PractitionerSummary(ORMModel):
    id: uuid.UUID
    display_name: str


class PatientSummary(ORMModel):
    """Minimal patient view. Never includes DOB, mobile or email."""

    id: uuid.UUID
    first_name: str


class AppointmentRead(ORMModel):
    id: uuid.UUID
    start_time: datetime
    end_time: datetime
    status: AppointmentStatus
    booking_source: BookingSource
    practitioner: PractitionerSummary
    patient: PatientSummary
    location_id: uuid.UUID
    appointment_type_id: uuid.UUID
    reason_summary: str | None = None


class AppointmentCreate(BaseModel):
    patient_id: uuid.UUID
    slot_id: uuid.UUID
    booking_source: BookingSource = BookingSource.ONLINE
    reason_summary: str | None = Field(default=None, max_length=280)
    #: Supply on retries so a timed-out request cannot double-book.
    idempotency_key: str | None = Field(default=None, min_length=8, max_length=80)
    is_new_patient: bool = False

    @field_validator("reason_summary")
    @classmethod
    def _strip(cls, value: str | None) -> str | None:
        return value.strip() or None if value else None


class AppointmentCancel(BaseModel):
    reason: str | None = Field(default=None, max_length=500)


class AppointmentReschedule(BaseModel):
    new_slot_id: uuid.UUID
    idempotency_key: str | None = Field(default=None, min_length=8, max_length=80)

    @model_validator(mode="after")
    def _check(self) -> AppointmentReschedule:
        return self
