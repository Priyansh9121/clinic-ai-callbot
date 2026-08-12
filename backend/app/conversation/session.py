"""Conversation session — the structured memory of a call.

This *is* the conversation memory. The engine never replays a transcript: every
decision is made from the fields below, which is what keeps later LLM prompts
small (only the fields relevant to the current decision are sent) and what
makes a correction cheap — changing one field re-enters the flow at the right
point instead of restarting it.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, time

from pydantic import BaseModel, ConfigDict, Field

from app.conversation.states import (
    ConversationState,
    Intent,
    PatientType,
    TransferReason,
    VerificationLevel,
)
from app.core.timeutils import DayPeriod, utc_now


class BookingDraft(BaseModel):
    """Slots being filled for a booking. Nothing here is authoritative until
    ``AppointmentService.book`` commits."""

    appointment_type_id: uuid.UUID | None = None
    appointment_type_name: str | None = None
    practitioner_id: uuid.UUID | None = None
    practitioner_name: str | None = None
    #: True when the caller explicitly said any practitioner is fine. Distinct
    #: from "not asked yet", which is what a bare None would mean.
    practitioner_any: bool = False
    preferred_date: date | None = None
    #: Set instead of ``preferred_date`` for range expressions ("next week"),
    #: where the caller named a window rather than a day.
    date_from: date | None = None
    date_to: date | None = None
    preferred_period: DayPeriod = DayPeriod.ANY
    #: From phrasings like "after 3"; clinic-local time.
    not_before: time | None = None

    #: Slots read out to the caller this turn, in the order spoken.
    offered_slot_ids: list[uuid.UUID] = Field(default_factory=list)
    selected_slot_id: uuid.UUID | None = None

    def clear_offers(self) -> None:
        """Called whenever a preference changes — previously offered slots no
        longer reflect what the caller asked for."""
        self.offered_slot_ids = []
        self.selected_slot_id = None

    @property
    def has_date_preference(self) -> bool:
        return self.preferred_date is not None or self.date_from is not None


class NewPatientDraft(BaseModel):
    """Details collected from a caller who is not yet a patient."""

    first_name: str | None = None
    last_name: str | None = None
    date_of_birth: date | None = None
    mobile: str | None = None

    @property
    def is_complete(self) -> bool:
        return all((self.first_name, self.last_name, self.date_of_birth, self.mobile))


class ConversationSession(BaseModel):
    """Transient per-call state, persisted in Redis with a TTL.

    Kept JSON-serialisable so the store backend is interchangeable.
    """

    model_config = ConfigDict(validate_assignment=True)

    session_id: str
    clinic_id: uuid.UUID
    call_id: uuid.UUID | None = None

    state: ConversationState = ConversationState.GREETING
    intent: Intent = Intent.UNKNOWN
    intent_confidence: float = 0.0

    #: Whether the caller has been told they are speaking to an automated
    #: assistant. Required disclosure — checked, not assumed.
    ai_disclosed: bool = False

    patient_type: PatientType | None = None
    patient_id: uuid.UUID | None = None
    patient_first_name: str | None = None
    verification_level: VerificationLevel = VerificationLevel.NONE
    verification_attempts: int = 0
    #: Identity details offered by the caller, awaiting a match.
    identity_last_name: str | None = None
    identity_dob: date | None = None

    booking: BookingDraft = Field(default_factory=BookingDraft)
    new_patient: NewPatientDraft = Field(default_factory=NewPatientDraft)

    #: Appointment targeted by a cancel or reschedule.
    target_appointment_id: uuid.UUID | None = None
    #: Appointments read out for the caller to choose between.
    offered_appointment_ids: list[uuid.UUID] = Field(default_factory=list)
    #: Appointment actually created this call.
    booked_appointment_id: uuid.UUID | None = None

    turns: int = 0
    retry_count: int = 0
    misunderstanding_count: int = 0

    emergency_triggered: bool = False
    requires_human: bool = False
    transfer_reason: TransferReason | None = None

    #: Idempotency key minted once per booking attempt, so a retry after a
    #: timeout cannot create a second appointment.
    booking_idempotency_key: str | None = None

    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    # --- Helpers -----------------------------------------------------------
    @property
    def is_terminal(self) -> bool:
        from app.conversation.states import TERMINAL_STATES

        return self.state in TERMINAL_STATES

    @property
    def is_identified(self) -> bool:
        return self.patient_id is not None and self.verification_level != VerificationLevel.NONE

    def touch(self) -> None:
        self.updated_at = utc_now()

    def note_misunderstanding(self) -> int:
        self.misunderstanding_count += 1
        self.retry_count += 1
        return self.misunderstanding_count

    def clear_retries(self) -> None:
        self.retry_count = 0

    def transfer(self, reason: TransferReason) -> None:
        self.requires_human = True
        self.transfer_reason = reason
        self.state = ConversationState.HUMAN_TRANSFER

    def memory_snapshot(self) -> dict[str, object]:
        """Compact view for logs, the dashboard and (later) LLM context.

        Contains no names, contact details or dates of birth — only ids and
        workflow state.
        """
        return {
            "state": self.state.value,
            "intent": self.intent.value,
            "identified": self.is_identified,
            "verification": self.verification_level.value,
            "patient_type": self.patient_type.value if self.patient_type else None,
            "appointment": {
                "type_id": str(self.booking.appointment_type_id)
                if self.booking.appointment_type_id
                else None,
                "practitioner_id": str(self.booking.practitioner_id)
                if self.booking.practitioner_id
                else None,
                "practitioner_any": self.booking.practitioner_any,
                "preferred_date": self.booking.preferred_date.isoformat()
                if self.booking.preferred_date
                else None,
                "preferred_period": self.booking.preferred_period.value,
                "offered": len(self.booking.offered_slot_ids),
            },
            "turns": self.turns,
            "misunderstandings": self.misunderstanding_count,
        }
