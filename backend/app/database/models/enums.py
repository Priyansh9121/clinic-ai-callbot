"""Persisted enumerations.

These are stored as native PostgreSQL enum types. Adding a value therefore
requires a migration, which is intentional: appointment and slot lifecycles are
safety-relevant and must not drift silently between code and database.
"""

from __future__ import annotations

from enum import StrEnum

import sqlalchemy as sa


class SlotStatus(StrEnum):
    AVAILABLE = "AVAILABLE"
    HELD = "HELD"  # transiently reserved mid-conversation
    BOOKED = "BOOKED"
    BLOCKED = "BLOCKED"  # leave, admin block, closed period


class AppointmentStatus(StrEnum):
    CONFIRMED = "CONFIRMED"
    CANCELLED = "CANCELLED"
    COMPLETED = "COMPLETED"
    NO_SHOW = "NO_SHOW"


class BookingSource(StrEnum):
    RECEPTION = "RECEPTION"
    ONLINE = "ONLINE"
    AI_CALLBOT = "AI_CALLBOT"


class CallOutcome(StrEnum):
    IN_PROGRESS = "IN_PROGRESS"
    BOOKED = "BOOKED"
    RESCHEDULED = "RESCHEDULED"
    CANCELLED = "CANCELLED"
    INFORMATION_PROVIDED = "INFORMATION_PROVIDED"
    TRANSFERRED = "TRANSFERRED"
    EMERGENCY_REDIRECTED = "EMERGENCY_REDIRECTED"
    ABANDONED = "ABANDONED"
    FAILED = "FAILED"


class CallEventType(StrEnum):
    """Structured events. Deliberately *not* a raw transcript stream."""

    CALL_STARTED = "CALL_STARTED"
    CALL_ENDED = "CALL_ENDED"
    STATE_CHANGED = "STATE_CHANGED"
    INTENT_DETECTED = "INTENT_DETECTED"
    ENTITY_CAPTURED = "ENTITY_CAPTURED"
    SAFETY_TRIGGERED = "SAFETY_TRIGGERED"
    ESCALATION = "ESCALATION"
    MISUNDERSTOOD = "MISUNDERSTOOD"
    TOOL_INVOKED = "TOOL_INVOKED"
    BOOKING_CONFIRMED = "BOOKING_CONFIRMED"
    BOOKING_FAILED = "BOOKING_FAILED"
    VERIFICATION_ATTEMPT = "VERIFICATION_ATTEMPT"
    TRANSFER_INITIATED = "TRANSFER_INITIATED"
    TRANSCRIPT_TURN = "TRANSCRIPT_TURN"  # only when RETAIN_TRANSCRIPTS is enabled


class AuditAction(StrEnum):
    APPOINTMENT_CREATED = "APPOINTMENT_CREATED"
    APPOINTMENT_CANCELLED = "APPOINTMENT_CANCELLED"
    APPOINTMENT_RESCHEDULED = "APPOINTMENT_RESCHEDULED"
    PATIENT_LOOKUP = "PATIENT_LOOKUP"
    PATIENT_VERIFICATION_SUCCEEDED = "PATIENT_VERIFICATION_SUCCEEDED"
    PATIENT_VERIFICATION_FAILED = "PATIENT_VERIFICATION_FAILED"
    EMERGENCY_TRIGGERED = "EMERGENCY_TRIGGERED"
    HUMAN_TRANSFER = "HUMAN_TRANSFER"
    CONFIG_CHANGED = "CONFIG_CHANGED"
    AUTH_FAILED = "AUTH_FAILED"


class ActorType(StrEnum):
    AI_CALLBOT = "AI_CALLBOT"
    STAFF = "STAFF"
    SYSTEM = "SYSTEM"
    PATIENT = "PATIENT"


def pg_enum(enum_cls: type[StrEnum], name: str) -> sa.Enum:
    """Build a native PG enum that stores the member *value*."""
    return sa.Enum(
        enum_cls,
        name=name,
        native_enum=True,
        values_callable=lambda e: [m.value for m in e],
        validate_strings=True,
    )
