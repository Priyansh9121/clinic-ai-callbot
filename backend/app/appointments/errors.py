"""Appointment-domain errors.

Public messages are deliberately identical for "does not exist" and "not
yours". A caller must not be able to probe for the existence of another
patient's appointment by observing different responses.
"""

from __future__ import annotations

from app.core.errors import NotFoundError


class AppointmentNotFoundError(NotFoundError):
    code = "appointment_not_found"
    public_message = "I couldn't find that appointment."


class SlotNotFoundError(NotFoundError):
    code = "slot_not_found"
    public_message = "That time is no longer available."


class PatientNotFoundError(NotFoundError):
    code = "patient_not_found"
    # Never confirms whether the patient record exists.
    public_message = "I couldn't verify those details."
