"""Audit event model.

Append-only by convention: nothing in the application updates or deletes rows
here. Every sensitive operation (booking, cancellation, patient lookup,
verification attempt, emergency trigger, transfer) writes one row.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base, UUIDPrimaryKeyMixin
from app.database.models.enums import ActorType, AuditAction, pg_enum


class AuditEvent(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "audit_events"
    __table_args__ = (
        Index("ix_audit_events_clinic_occurred", "clinic_id", "occurred_at"),
        Index("ix_audit_events_action_occurred", "action", "occurred_at"),
        Index("ix_audit_events_subject", "subject_type", "subject_id"),
    )

    clinic_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("clinics.id", ondelete="SET NULL")
    )
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    action: Mapped[AuditAction] = mapped_column(
        pg_enum(AuditAction, "audit_action"), nullable=False
    )

    actor_type: Mapped[ActorType] = mapped_column(pg_enum(ActorType, "actor_type"), nullable=False)
    # Staff username / session id / "system". Never a patient's name.
    actor_ref: Mapped[str | None] = mapped_column(String(120))

    # What the action was performed on, e.g. ("appointment", <uuid>).
    subject_type: Mapped[str | None] = mapped_column(String(48))
    subject_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True))

    success: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # Structured, non-identifying context (reason codes, counts, durations).
    metadata_json: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, nullable=False, default=dict
    )
