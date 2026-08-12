"""Call metadata persistence.

Writes the minimum needed to run and review the service: one ``calls`` row per
conversation plus structured ``call_events``. No audio, and no transcript
unless ``RETAIN_TRANSCRIPTS`` is explicitly enabled.
"""

from __future__ import annotations

import hashlib
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.conversation.session import ConversationSession
from app.core.config import Settings
from app.core.logging import get_logger
from app.core.metrics import M, metrics
from app.core.timeutils import utc_now
from app.database.models.call import Call, CallEvent
from app.database.models.enums import CallEventType, CallOutcome

logger = get_logger(__name__)


class CallService:
    def __init__(self, session: AsyncSession, settings: Settings) -> None:
        self._session = session
        self._settings = settings

    @staticmethod
    def hash_caller_id(caller_id: str | None, salt: str) -> str | None:
        """One-way hash so repeat callers can be correlated without storing
        the number itself."""
        if not caller_id:
            return None
        return hashlib.sha256(f"{salt}:{caller_id}".encode()).hexdigest()

    async def start_call(
        self,
        *,
        clinic_id: uuid.UUID,
        session_id: str,
        channel: str = "text",
        caller_id: str | None = None,
    ) -> Call:
        call = Call(
            clinic_id=clinic_id,
            session_id=session_id,
            started_at=utc_now(),
            channel=channel,
            caller_hash=self.hash_caller_id(
                caller_id, self._settings.app_secret_key.get_secret_value()
            ),
            outcome=CallOutcome.IN_PROGRESS,
        )
        self._session.add(call)
        await self._session.flush()
        self._session.add(
            CallEvent(
                call_id=call.id,
                occurred_at=utc_now(),
                event_type=CallEventType.CALL_STARTED,
                payload={"channel": channel},
            )
        )
        return call

    async def get_call(self, call_id: uuid.UUID) -> Call | None:
        return await self._session.get(Call, call_id)

    async def get_by_session(self, session_id: str) -> Call | None:
        result = await self._session.execute(select(Call).where(Call.session_id == session_id))
        return result.scalars().first()

    def record_event(
        self,
        call_id: uuid.UUID,
        event_type: CallEventType,
        payload: dict[str, object] | None = None,
    ) -> None:
        self._session.add(
            CallEvent(
                call_id=call_id,
                occurred_at=utc_now(),
                event_type=event_type,
                payload=payload or {},
            )
        )

    def record_turn_text(self, call_id: uuid.UUID, *, caller: str, reply: str) -> None:
        """Store the turn text — only when retention is explicitly enabled."""
        if not self._settings.retain_transcripts:
            return
        self._session.add(
            CallEvent(
                call_id=call_id,
                occurred_at=utc_now(),
                event_type=CallEventType.TRANSCRIPT_TURN,
                payload={"caller": caller[:500], "reply": reply[:500]},
            )
        )

    async def sync_from_session(
        self, call: Call, session: ConversationSession, *, outcome: CallOutcome | None = None
    ) -> None:
        """Mirror the conversation state onto the persisted call row."""
        call.intent = session.intent.value
        call.final_state = session.state.value
        call.turn_count = session.turns
        call.misunderstanding_count = session.misunderstanding_count
        call.human_transfer = session.requires_human
        call.transfer_reason = session.transfer_reason.value if session.transfer_reason else None
        call.emergency_triggered = session.emergency_triggered
        if session.booked_appointment_id is not None:
            call.appointment_id = session.booked_appointment_id
        if outcome is not None:
            call.outcome = outcome

    async def end_call(
        self, call: Call, session: ConversationSession, outcome: CallOutcome
    ) -> None:
        await self.sync_from_session(call, session, outcome=outcome)
        call.ended_at = utc_now()
        self.record_event(call.id, CallEventType.CALL_ENDED, {"outcome": outcome.value})
        duration_ms = (call.ended_at - call.started_at).total_seconds() * 1000
        metrics.observe(M.CALL_DURATION, duration_ms)
        metrics.observe(M.CALL_TURNS, float(session.turns))
        logger.info(
            "call.ended",
            outcome=outcome.value,
            turns=session.turns,
            transferred=session.requires_human,
        )


def outcome_for(session: ConversationSession) -> CallOutcome:
    """Derive the reportable outcome from the final session state."""
    from app.conversation.states import ConversationState, Intent

    if session.emergency_triggered:
        return CallOutcome.EMERGENCY_REDIRECTED
    if session.requires_human:
        return CallOutcome.TRANSFERRED
    if session.booked_appointment_id is not None:
        return (
            CallOutcome.RESCHEDULED
            if session.intent is Intent.RESCHEDULE_APPOINTMENT
            else CallOutcome.BOOKED
        )
    if session.intent is Intent.CANCEL_APPOINTMENT and session.state in (
        ConversationState.BOOKING_COMPLETE,
        ConversationState.END_CALL,
    ):
        return CallOutcome.CANCELLED
    if session.intent is Intent.CLINIC_INFORMATION:
        return CallOutcome.INFORMATION_PROVIDED
    if session.state is ConversationState.END_CALL:
        return CallOutcome.ABANDONED
    return CallOutcome.IN_PROGRESS
