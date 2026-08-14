"""Appointment type model."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, CheckConstraint, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.database.models.clinic import Clinic


class AppointmentType(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "appointment_types"
    __table_args__ = (
        UniqueConstraint("clinic_id", "slug", name="uq_appointment_types_clinic_id_slug"),
        CheckConstraint("duration_minutes > 0", name="duration_positive"),
    )

    clinic_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("clinics.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # Stable machine key used by the conversation engine, so renaming the
    # spoken name never changes behaviour.
    slug: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    duration_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=15)
    new_patient_allowed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    telehealth_allowed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    requires_referral: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    clinic: Mapped[Clinic] = relationship(back_populates="appointment_types")
