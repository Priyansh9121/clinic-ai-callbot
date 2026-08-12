"""Appointment endpoints.

Writes require a staff credential. The AI callbot does **not** use these
endpoints — it calls the domain services directly, in-process, with the
conversation session as the actor. That separation means an API key leak cannot
be used to drive the callbot, and a prompt injection cannot reach the API.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends

from app.api.deps import AppointmentSvc, ClinicSvc
from app.appointments.service import BookingRequest
from app.schemas.appointment import (
    AppointmentCancel,
    AppointmentCreate,
    AppointmentRead,
    AppointmentReschedule,
)
from app.security.auth import Principal, Role, require_role

router = APIRouter(tags=["appointments"])

StaffPrincipal = Annotated[Principal, Depends(require_role(Role.STAFF))]


@router.post("/appointments", response_model=AppointmentRead, status_code=201)
async def create_appointment(
    payload: AppointmentCreate,
    service: AppointmentSvc,
    clinic_service: ClinicSvc,
    principal: StaffPrincipal,
) -> AppointmentRead:
    clinic = await clinic_service.get_default_clinic()
    result = await service.book(
        BookingRequest(
            clinic_id=clinic.id,
            patient_id=payload.patient_id,
            slot_id=payload.slot_id,
            source=payload.booking_source,
            reason_summary=payload.reason_summary,
            idempotency_key=payload.idempotency_key,
            is_new_patient=payload.is_new_patient,
            actor_ref=principal.identifier,
        )
    )
    return AppointmentRead.model_validate(result.appointment)


@router.get("/appointments/{appointment_id}", response_model=AppointmentRead)
async def get_appointment(
    appointment_id: uuid.UUID, service: AppointmentSvc, principal: StaffPrincipal
) -> AppointmentRead:
    appointment = await service.get(appointment_id)
    return AppointmentRead.model_validate(appointment)


@router.post("/appointments/{appointment_id}/cancel", response_model=AppointmentRead)
async def cancel_appointment(
    appointment_id: uuid.UUID,
    payload: AppointmentCancel,
    service: AppointmentSvc,
    principal: StaffPrincipal,
) -> AppointmentRead:
    appointment = await service.cancel(
        appointment_id, reason=payload.reason, actor_ref=principal.identifier
    )
    return AppointmentRead.model_validate(appointment)


@router.post("/appointments/{appointment_id}/reschedule", response_model=AppointmentRead)
async def reschedule_appointment(
    appointment_id: uuid.UUID,
    payload: AppointmentReschedule,
    service: AppointmentSvc,
    principal: StaffPrincipal,
) -> AppointmentRead:
    appointment = await service.reschedule(
        appointment_id,
        payload.new_slot_id,
        actor_ref=principal.identifier,
        idempotency_key=payload.idempotency_key,
    )
    return AppointmentRead.model_validate(appointment)
