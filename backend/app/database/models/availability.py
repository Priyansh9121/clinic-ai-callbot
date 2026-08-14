"""Availability slot model.

The slot table is the concurrency control point for booking. A slot row is
locked with ``SELECT ... FOR UPDATE`` before an appointment is written, and the
database constraints below make a double booking impossible even if a future
code path forgets to take the lock.
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
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.database.models.enums import SlotStatus, pg_enum

if TYPE_CHECKING:
    from app.database.models.appointment_type import AppointmentType
    from app.database.models.clinic import Location
    from app.database.models.practitioner import Practitioner


class AvailabilitySlot(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "availability_slots"
    __table_args__ = (
        # A practitioner cannot have two slots starting at the same instant.
        UniqueConstraint(
            "practitioner_id", "start_time", name="uq_availability_slots_practitioner_id_start_time"
        ),
        CheckConstraint("end_time > start_time", name="slot_end_after_start"),
        # Primary search path: open slots for a type/practitioner in a window.
        Index(
            "ix_availability_slots_search",
            "appointment_type_id",
            "status",
            "start_time",
            "practitioner_id",
        ),
        Index("ix_availability_slots_status_start", "status", "start_time"),
    )

    practitioner_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("practitioners.id", ondelete="CASCADE"), nullable=False
    )
    location_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("locations.id", ondelete="CASCADE"), nullable=False
    )
    appointment_type_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("appointment_types.id", ondelete="CASCADE"), nullable=False
    )
    start_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    end_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[SlotStatus] = mapped_column(
        pg_enum(SlotStatus, "slot_status"), nullable=False, default=SlotStatus.AVAILABLE
    )
    # Set while a slot is HELD so an abandoned conversation cannot strand it.
    held_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    practitioner: Mapped[Practitioner] = relationship(lazy="joined")
    location: Mapped[Location] = relationship(lazy="joined")
    appointment_type: Mapped[AppointmentType] = relationship(lazy="joined")

    @property
    def is_bookable(self) -> bool:
        return self.status == SlotStatus.AVAILABLE
