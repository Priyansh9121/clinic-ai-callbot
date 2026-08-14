"""Patient model.

Everything on this table is sensitive personal/health-adjacent information.
Rules that the rest of the codebase relies on:

* No API response serialises this model directly — see ``app.schemas.patient``,
  which exposes only a first name and a masked contact hint.
* Lookups go through ``app.patients.service`` which never confirms or denies
  the existence of a record to an unverified caller (no enumeration).
* ``mobile_normalised`` exists so lookups are exact-match on a canonical form
  rather than doing fuzzy matching on user-entered text.
"""

from __future__ import annotations

import uuid
from datetime import date

from sqlalchemy import Boolean, Date, ForeignKey, Index, String
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class Patient(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "patients"
    __table_args__ = (
        # Supports the verification lookup (surname + DOB) without scanning.
        Index("ix_patients_clinic_lastname_dob", "clinic_id", "last_name", "date_of_birth"),
        Index("ix_patients_clinic_mobile", "clinic_id", "mobile_normalised"),
    )

    clinic_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("clinics.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    first_name: Mapped[str] = mapped_column(String(100), nullable=False)
    last_name: Mapped[str] = mapped_column(String(100), nullable=False)
    date_of_birth: Mapped[date] = mapped_column(Date, nullable=False)
    mobile: Mapped[str | None] = mapped_column(String(32))
    # E.164 form of `mobile`, maintained by the patient service.
    mobile_normalised: Mapped[str | None] = mapped_column(String(20))
    email: Mapped[str | None] = mapped_column(String(254))
    preferred_language: Mapped[str] = mapped_column(String(16), nullable=False, default="en-AU")
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}"
