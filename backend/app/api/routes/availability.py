"""Availability search endpoint."""

from __future__ import annotations

import uuid
from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.api.deps import AvailabilitySvc, ClinicSvc
from app.appointments.availability import AvailabilityQuery
from app.clinic.service import ClinicService
from app.core.timeutils import DayPeriod
from app.schemas.appointment import AvailabilityQueryParams, SlotRead

router = APIRouter(tags=["availability"])


def _params(
    appointment_type_id: uuid.UUID | None = Query(default=None),
    practitioner_id: uuid.UUID | None = Query(default=None),
    location_id: uuid.UUID | None = Query(default=None),
    day: date | None = Query(default=None),
    period: DayPeriod = Query(default=DayPeriod.ANY),
    limit: int = Query(default=20, ge=1, le=100),
) -> AvailabilityQueryParams:
    """Validate query parameters through the schema rather than inline."""
    return AvailabilityQueryParams(
        appointment_type_id=appointment_type_id,
        practitioner_id=practitioner_id,
        location_id=location_id,
        day=day,
        period=period,
        limit=limit,
    )


@router.get("/availability", response_model=list[SlotRead])
async def search_availability(
    clinic_service: ClinicSvc,
    availability: AvailabilitySvc,
    params: Annotated[AvailabilityQueryParams, Depends(_params)],
    clinic_id: uuid.UUID | None = Query(default=None),
) -> list[SlotRead]:
    clinic = (
        await clinic_service.get_clinic(clinic_id)
        if clinic_id
        else await clinic_service.get_default_clinic()
    )
    slots = await availability.search(
        AvailabilityQuery(
            clinic_id=clinic.id,
            appointment_type_id=params.appointment_type_id,
            practitioner_id=params.practitioner_id,
            location_id=params.location_id,
            day=params.day,
            period=params.period,
            limit=params.limit,
        ),
        ClinicService.timezone_of(clinic),
    )
    return [SlotRead.model_validate(slot) for slot in slots]
