"""SQLAlchemy models.

Imported as a package so that ``Base.metadata`` is fully populated before
Alembic autogenerate or ``create_all`` runs.
"""

from app.database.models.appointment import Appointment
from app.database.models.appointment_type import AppointmentType
from app.database.models.audit import AuditEvent
from app.database.models.availability import AvailabilitySlot
from app.database.models.call import Call, CallEvent
from app.database.models.clinic import Clinic, Location
from app.database.models.enums import (
    ActorType,
    AppointmentStatus,
    AuditAction,
    BookingSource,
    CallEventType,
    CallOutcome,
    SlotStatus,
)
from app.database.models.patient import Patient
from app.database.models.practitioner import Practitioner

__all__ = [
    "ActorType",
    "Appointment",
    "AppointmentStatus",
    "AppointmentType",
    "AuditAction",
    "AuditEvent",
    "AvailabilitySlot",
    "BookingSource",
    "Call",
    "CallEvent",
    "CallEventType",
    "CallOutcome",
    "Clinic",
    "Location",
    "Patient",
    "Practitioner",
    "SlotStatus",
]
