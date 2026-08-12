"""Clinic directory endpoints (read-only, public)."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Query

from app.api.deps import ClinicSvc
from app.schemas.clinic import (
    AppointmentTypeRead,
    ClinicRead,
    LocationRead,
    PractitionerRead,
)

router = APIRouter(tags=["clinic"])


@router.get("/clinic", response_model=ClinicRead)
async def get_clinic(service: ClinicSvc) -> ClinicRead:
    clinic = await service.get_default_clinic()
    return ClinicRead.model_validate(clinic)


@router.get("/locations", response_model=list[LocationRead])
async def list_locations(
    service: ClinicSvc, clinic_id: uuid.UUID | None = Query(default=None)
) -> list[LocationRead]:
    clinic = (
        await service.get_clinic(clinic_id) if clinic_id else await service.get_default_clinic()
    )
    locations = await service.list_locations(clinic.id)
    return [LocationRead.model_validate(loc) for loc in locations]


@router.get("/practitioners", response_model=list[PractitionerRead])
async def list_practitioners(
    service: ClinicSvc,
    clinic_id: uuid.UUID | None = Query(default=None),
    accepting_new_patients: bool | None = Query(default=None),
) -> list[PractitionerRead]:
    clinic = (
        await service.get_clinic(clinic_id) if clinic_id else await service.get_default_clinic()
    )
    practitioners = await service.list_practitioners(
        clinic.id, accepting_new_patients=accepting_new_patients
    )
    return [PractitionerRead.model_validate(p) for p in practitioners]


@router.get("/appointment-types", response_model=list[AppointmentTypeRead])
async def list_appointment_types(
    service: ClinicSvc,
    clinic_id: uuid.UUID | None = Query(default=None),
    new_patient_only: bool = Query(default=False),
) -> list[AppointmentTypeRead]:
    clinic = (
        await service.get_clinic(clinic_id) if clinic_id else await service.get_default_clinic()
    )
    types = await service.list_appointment_types(clinic.id, new_patient_only=new_patient_only)
    return [AppointmentTypeRead.model_validate(t) for t in types]
