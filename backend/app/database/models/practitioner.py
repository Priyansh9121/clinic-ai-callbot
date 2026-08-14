"""Practitioner model."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, ForeignKey, String
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.database.models.clinic import Clinic, Location


class Practitioner(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "practitioners"

    clinic_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("clinics.id", ondelete="CASCADE"), nullable=False,
        index=True,
    )
    # Primary/home location. A practitioner may still have slots elsewhere;
    # the slot itself carries the authoritative location.
    location_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("locations.id", ondelete="SET NULL"), index=True
    )
    first_name: Mapped[str] = mapped_column(String(100), nullable=False)
    last_name: Mapped[str] = mapped_column(String(100), nullable=False)
    # What the callbot says aloud, e.g. "Dr Sarah Patel".
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    speciality: Mapped[str | None] = mapped_column(String(120))
    accepts_new_patients: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    clinic: Mapped[Clinic] = relationship(back_populates="practitioners")
    location: Mapped[Location | None] = relationship(lazy="joined")
