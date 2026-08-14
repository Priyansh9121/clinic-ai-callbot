"""Call and call-event models.

Data minimisation (see docs/privacy/data-inventory.md):

* No audio is referenced or stored.
* ``call_events`` holds *structured* events — state transitions, detected
  intents, tool invocations — not a running transcript. A per-turn text event
  is only written when ``RETAIN_TRANSCRIPTS`` is explicitly enabled.
* The caller's phone number is stored as a one-way hash by default so repeat
  callers can be correlated for abuse control without retaining the number.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.database.models.enums import CallEventType, CallOutcome, pg_enum


class Call(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "calls"
    __table_args__ = (Index("ix_calls_clinic_started", "clinic_id", "started_at"),)

    clinic_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("clinics.id", ondelete="CASCADE"), nullable=False
    )
    # Conversation session key (also the Redis key). Lets a live session be
    # correlated with its persisted call without exposing internal ids.
    session_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)

    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # "text" for the simulator/API, "voice" once telephony is connected.
    channel: Mapped[str] = mapped_column(String(16), nullable=False, default="text")
    # SHA-256 of the caller id + clinic salt; never the raw number.
    caller_hash: Mapped[str | None] = mapped_column(String(64), index=True)

    intent: Mapped[str | None] = mapped_column(String(48))
    outcome: Mapped[CallOutcome] = mapped_column(
        pg_enum(CallOutcome, "call_outcome"), nullable=False, default=CallOutcome.IN_PROGRESS
    )
    final_state: Mapped[str | None] = mapped_column(String(48))

    human_transfer: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    transfer_reason: Mapped[str | None] = mapped_column(String(64))
    emergency_triggered: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    turn_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    misunderstanding_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Resulting appointment, when the call produced one.
    appointment_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("appointments.id", ondelete="SET NULL")
    )

    events: Mapped[list[CallEvent]] = relationship(
        back_populates="call", cascade="all, delete-orphan", order_by="CallEvent.occurred_at"
    )

    @property
    def duration_seconds(self) -> float | None:
        if self.ended_at is None:
            return None
        return (self.ended_at - self.started_at).total_seconds()


class CallEvent(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "call_events"
    __table_args__ = (Index("ix_call_events_call_occurred", "call_id", "occurred_at"),)

    call_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("calls.id", ondelete="CASCADE"), nullable=False
    )
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    event_type: Mapped[CallEventType] = mapped_column(
        pg_enum(CallEventType, "call_event_type"), nullable=False
    )
    # Bounded, structured payload. Must not contain patient identifiers; the
    # event writer in app.conversation.engine enforces this.
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)

    call: Mapped[Call] = relationship(back_populates="events")
