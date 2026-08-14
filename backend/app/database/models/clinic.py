"""Clinic and location models."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.database.models.appointment_type import AppointmentType
    from app.database.models.practitioner import Practitioner


class Clinic(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "clinics"

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    phone: Mapped[str | None] = mapped_column(String(32))
    email: Mapped[str | None] = mapped_column(String(254))
    # IANA timezone name. Authoritative for every appointment time spoken to a
    # caller — the application never relies on the server's local timezone.
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, default="Australia/Melbourne")
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    locations: Mapped[list[Location]] = relationship(
        back_populates="clinic", cascade="all, delete-orphan", lazy="selectin"
    )
    practitioners: Mapped[list[Practitioner]] = relationship(
        back_populates="clinic", cascade="all, delete-orphan"
    )
    appointment_types: Mapped[list[AppointmentType]] = relationship(
        back_populates="clinic", cascade="all, delete-orphan"
    )


class Location(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "locations"

    clinic_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("clinics.id", ondelete="CASCADE"), nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    address: Mapped[str | None] = mapped_column(Text)
    phone: Mapped[str | None] = mapped_column(String(32))
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    clinic: Mapped[Clinic] = relationship(back_populates="locations")
