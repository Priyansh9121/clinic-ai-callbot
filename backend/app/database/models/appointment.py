"""Appointment model.

Integrity guarantees are expressed as database constraints, not as application
checks, so that a bug in a future code path cannot produce a double booking:

* ``uq_appointments_active_slot``      one live appointment per slot
* ``uq_appointments_active_prac_time`` one live appointment per practitioner/time
* ``uq_appointments_active_pat_time``  a patient cannot be in two places at once
* ``uq_appointments_idempotency_key``  a retried booking request is a no-op

The three "active" indexes are partial (``WHERE status = 'CONFIRMED'``) so that
cancelling an appointment frees the slot for rebooking while the cancelled row
is retained for audit.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.database.models.enums import AppointmentStatus, BookingSource, pg_enum

if TYPE_CHECKING:
    from app.database.models.appointment_type import AppointmentType
    from app.database.models.availability import AvailabilitySlot
    from app.database.models.clinic import Location
    from app.database.models.patient import Patient
    from app.database.models.practitioner import Practitioner

_ACTIVE = text("status = 'CONFIRMED'")


class Appointment(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "appointments"
    __table_args__ = (
        Index(
            "uq_appointments_active_slot",
            "availability_slot_id",
            unique=True,
            postgresql_where=_ACTIVE,
        ),
        Index(
            "uq_appointments_active_prac_time",
            "practitioner_id",
            "start_time",
            unique=True,
            postgresql_where=_ACTIVE,
        ),
        Index(
            "uq_appointments_active_pat_time",
            "patient_id",
            "start_time",
            unique=True,
            postgresql_where=_ACTIVE,
        ),
        Index(
            "uq_appointments_idempotency_key",
            "idempotency_key",
            unique=True,
            postgresql_where=text("idempotency_key IS NOT NULL"),
        ),
        Index("ix_appointments_patient_start", "patient_id", "start_time"),
        Index("ix_appointments_clinic_start", "clinic_id", "start_time"),
        CheckConstraint("end_time > start_time", name="appointment_end_after_start"),
    )

    clinic_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("clinics.id", ondelete="RESTRICT"), nullable=False
    )
    patient_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("patients.id", ondelete="RESTRICT"), nullable=False
    )
    practitioner_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("practitioners.id", ondelete="RESTRICT"), nullable=False
    )
    location_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("locations.id", ondelete="RESTRICT"), nullable=False
    )
    appointment_type_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("appointment_types.id", ondelete="RESTRICT"), nullable=False
    )
    availability_slot_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("availability_slots.id", ondelete="SET NULL")
    )

    start_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    end_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[AppointmentStatus] = mapped_column(
        pg_enum(AppointmentStatus, "appointment_status"),
        nullable=False,
        default=AppointmentStatus.CONFIRMED,
    )
    booking_source: Mapped[BookingSource] = mapped_column(
        pg_enum(BookingSource, "booking_source"), nullable=False, default=BookingSource.AI_CALLBOT
    )
    # A short, non-clinical note ("follow-up", "script renewal"). The callbot
    # never records symptom detail or anything resembling a clinical assessment.
    reason_summary: Mapped[str | None] = mapped_column(String(280))

    # Supplied by the caller of the booking service so a retry after a network
    # timeout returns the original appointment instead of creating a second one.
    idempotency_key: Mapped[str | None] = mapped_column(String(80))

    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancellation_reason: Mapped[str | None] = mapped_column(Text)
    # Set on the old appointment when it is superseded by a reschedule.
    rescheduled_to_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("appointments.id", ondelete="SET NULL")
    )

    patient: Mapped[Patient] = relationship(lazy="joined")
    practitioner: Mapped[Practitioner] = relationship(lazy="joined")
    location: Mapped[Location] = relationship(lazy="joined")
    appointment_type: Mapped[AppointmentType] = relationship(lazy="joined")
    availability_slot: Mapped[AvailabilitySlot | None] = relationship(
        foreign_keys=[availability_slot_id]
    )

    @property
    def is_active(self) -> bool:
        return self.status == AppointmentStatus.CONFIRMED
