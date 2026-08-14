"""Conversation engine.

Turn pipeline, in strict order:

1. **Safety** — deterministic rules, before any interpretation. An emergency
   leaves the workflow entirely and cannot be re-entered.
2. **Analysis** — intent and entities, resolved against clinic vocabulary.
   Never against free text that a downstream component would re-interpret.
3. **Memory update** — entities applied to the session; changed fields are
   recorded so flows can distinguish a correction from an answer.
4. **Routing** — explicit transfer requests and intent switches.
5. **Flow** — the handler for the active intent takes one step.
6. **Persistence** — session to the store, structured events to the database.

Everything a language model could later influence lives in step 2. Steps 1, 4
and 5 are code, so a model cannot introduce a state transition, a permission or
a database write that the machine does not already allow.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from datetime import date

from sqlalchemy.ext.asyncio import AsyncSession

from app.appointments.availability import AvailabilityService
from app.appointments.service import AppointmentService
from app.audit.service import AuditService
from app.clinic import directory
from app.clinic.knowledge import ClinicKnowledgeProvider
from app.clinic.service import ClinicService
from app.conversation import responses
from app.conversation.handlers import booking, cancel, information, reschedule
from app.conversation.handlers.base import FlowContext, Reply
from app.conversation.nlu.baseline import Analysis, MessageAnalyser
from app.conversation.session import ConversationSession
from app.conversation.states import (
    ConversationState,
    Intent,
    TransferReason,
)
from app.conversation.store import SessionStore
from app.conversation.vocabulary import load_vocabulary
from app.core.config import Settings
from app.core.errors import AppError
from app.core.logging import get_logger
from app.core.metrics import M, metrics
from app.core.timeutils import clinic_today
from app.database.models.enums import (
    ActorType,
    AuditAction,
    CallEventType,
    CallOutcome,
)
from app.patients.service import PatientService
from app.safety.engine import SafetyEngine, emergency_response
from app.safety.models import SafetyVerdict
from app.services.call_service import CallService, outcome_for

logger = get_logger(__name__)


@dataclass(slots=True)
class TurnResult:
    """What the caller hears, plus everything the API and dashboard need."""

    session_id: str
    reply: str
    state: ConversationState
    intent: Intent
    requires_human: bool = False
    emergency: bool = False
    ended: bool = False
    transfer_reason: str | None = None
    latency_ms: float = 0.0
    memory: dict[str, object] = field(default_factory=dict)


class ConversationEngine:
    """One instance per request/turn; holds no cross-call state itself."""

    def __init__(
        self,
        *,
        db: AsyncSession,
        settings: Settings,
        store: SessionStore,
        safety: SafetyEngine,
        analyser: MessageAnalyser,
        knowledge: ClinicKnowledgeProvider,
    ) -> None:
        self._db = db
        self._settings = settings
        self._store = store
        self._safety = safety
        self._analyser = analyser
        self._knowledge = knowledge

        self._clinic_service = ClinicService(db)
        self._availability_service = AvailabilityService(db, settings)
        self._appointment_service = AppointmentService(db, settings)
        self._patient_service = PatientService(db)
        self._audit_service = AuditService(db)
        self._call_service = CallService(db, settings)

    # --- Public API --------------------------------------------------------
    async def start(
        self,
        *,
        session_id: str | None = None,
        channel: str = "text",
        caller_id: str | None = None,
    ) -> TurnResult:
        """Open a call and return the greeting (with the required disclosure)."""
        clinic = await self._clinic_service.get_default_clinic()
        session_id = session_id or f"sess_{uuid.uuid4().hex[:16]}"

        session = ConversationSession(session_id=session_id, clinic_id=clinic.id)
        call = await self._call_service.start_call(
            clinic_id=clinic.id, session_id=session_id, channel=channel, caller_id=caller_id
        )
        session.call_id = call.id
        session.ai_disclosed = True
        session.state = ConversationState.IDENTIFY_INTENT
        await self._store.save(session)
        await self._db.commit()

        return TurnResult(
            session_id=session_id,
            reply=responses.GREETING_WITH_DISCLOSURE.format(clinic_name=clinic.name),
            state=session.state,
            intent=session.intent,
            memory=session.memory_snapshot(),
        )

    async def handle_message(
        self, session_id: str, message: str, *, channel: str = "text"
    ) -> TurnResult:
        started = time.perf_counter()
        session = await self._store.get(session_id)
        if session is None:
            # An unknown or expired session starts a fresh call rather than
            # erroring — a caller must never hit a dead end.
            greeting = await self.start(session_id=session_id, channel=channel)
            session = await self._store.get(session_id)
            if session is None:  # pragma: no cover - store failure
                return greeting

        clinic = await self._clinic_service.get_clinic(session.clinic_id)
        tz = ClinicService.timezone_of(clinic)
        today = clinic_today(tz)

        session.turns += 1
        metrics.increment(M.CONVERSATION_TURNS_TOTAL)

        try:
            reply = await self._run_turn(session, message, clinic=clinic, tz=tz, today=today)
        except AppError as exc:
            logger.warning("conversation.domain_error", code=exc.code)
            reply = Reply(text=responses.transfer_message(TransferReason.TECHNICAL_FAILURE))
            session.transfer(TransferReason.TECHNICAL_FAILURE)
        except Exception as exc:
            # A crash must degrade to a human, never to silence.
            logger.error("conversation.unhandled_error", error=type(exc).__name__, exc_info=True)
            await self._db.rollback()
            reply = Reply(text=responses.transfer_message(TransferReason.TECHNICAL_FAILURE))
            session.transfer(TransferReason.TECHNICAL_FAILURE)

        latency_ms = (time.perf_counter() - started) * 1000
        await self._persist(session, message, reply, latency_ms=latency_ms)

        metrics.observe(M.CONVERSATION_TURN, latency_ms)
        return TurnResult(
            session_id=session.session_id,
            reply=reply.text,
            state=session.state,
            intent=session.intent,
            requires_human=session.requires_human,
            emergency=session.emergency_triggered,
            ended=session.is_terminal,
            transfer_reason=session.transfer_reason.value if session.transfer_reason else None,
            latency_ms=round(latency_ms, 2),
            memory=session.memory_snapshot(),
        )

    async def get_session(self, session_id: str) -> ConversationSession | None:
        return await self._store.get(session_id)

    async def end(self, session_id: str) -> None:
        session = await self._store.get(session_id)
        if session is None:
            return
        call = await self._call_service.get_by_session(session_id)
        if call is not None and call.ended_at is None:
            await self._call_service.end_call(call, session, outcome_for(session))
            await self._db.commit()
        await self._store.delete(session_id)

    # --- Turn pipeline -----------------------------------------------------
    async def _run_turn(
        self, session: ConversationSession, message: str, *, clinic, tz, today: date
    ) -> Reply:
        # (1) Safety, before anything else.
        verdict = await self._safety.evaluate_async(message)
        if verdict.is_emergency:
            return self._enter_emergency(session, verdict)

        # A terminated call is not resumed by further input.
        if session.state is ConversationState.EMERGENCY:
            return Reply(text=emergency_response(verdict.category, self._settings.emergency_number))
        if session.state is ConversationState.HUMAN_TRANSFER:
            return Reply(
                text=responses.transfer_message(
                    session.transfer_reason or TransferReason.CALLER_REQUEST
                )
            )
        if session.state is ConversationState.END_CALL:
            return Reply(text=responses.GOODBYE)

        if verdict.requires_human:
            from app.safety.engine import escalation_response

            reason = (
                TransferReason.SAFETY
                if verdict.category.name in ("DISTRESS",)
                else TransferReason.CLINICAL_QUESTION
            )
            session.transfer(reason)
            self._audit_service.record(
                action=AuditAction.HUMAN_TRANSFER,
                actor_type=ActorType.AI_CALLBOT,
                clinic_id=clinic.id,
                actor_ref=session.session_id,
                metadata={
                    "reason": reason.value,
                    "rule_id": verdict.rule_id,
                    "category": verdict.category.value,
                },
            )
            return Reply(
                text=escalation_response(verdict.category),
                events=[
                    (
                        "SAFETY_TRIGGERED",
                        {"category": verdict.category.value, "rule_id": verdict.rule_id},
                    )
                ],
            )

        # (2) Analysis.
        vocabulary = await load_vocabulary(self._db, clinic.id)
        analysis = await self._analyser.analyse(
            message, vocabulary=vocabulary, today=today, state_hint=session.state.value
        )
        if analysis.intent is not Intent.UNKNOWN:
            metrics.increment(M.INTENT_DETECTED, labels={"intent": analysis.intent.value})
            metrics.observe(M.INTENT_CONFIDENCE, analysis.confidence * 100)

        # (3) Memory update.
        changed = self._apply_entities(session, analysis)

        ctx = FlowContext(
            session=session,
            analysis=analysis,
            clinic=clinic,
            tz=tz,
            today=today,
            settings=self._settings,
            vocabulary=vocabulary,
            clinic_service=self._clinic_service,
            availability_service=self._availability_service,
            appointment_service=self._appointment_service,
            patient_service=self._patient_service,
            audit_service=self._audit_service,
            knowledge=self._knowledge,
            changed=frozenset(changed),
        )

        # (4) Routing.
        routed = await self._route(ctx)
        if routed is not None:
            return routed

        # (5) Flow.
        return await self._dispatch(ctx)

    def _enter_emergency(self, session: ConversationSession, verdict: SafetyVerdict) -> Reply:
        session.emergency_triggered = True
        session.state = ConversationState.EMERGENCY
        session.requires_human = False  # emergency services, not reception
        self._audit_service.record(
            action=AuditAction.EMERGENCY_TRIGGERED,
            actor_type=ActorType.AI_CALLBOT,
            clinic_id=session.clinic_id,
            actor_ref=session.session_id,
            metadata={"rule_id": verdict.rule_id, "category": verdict.category.value},
        )
        return Reply(
            text=emergency_response(verdict.category, self._settings.emergency_number),
            events=[
                (
                    "SAFETY_TRIGGERED",
                    {"category": verdict.category.value, "rule_id": verdict.rule_id},
                )
            ],
        )

    async def _route(self, ctx: FlowContext) -> Reply | None:
        """Handle intent switches and requests that bypass the active flow."""
        session, analysis = ctx.session, ctx.analysis

        # An explicit request for a person is honoured immediately and is never
        # argued with, whatever the current state.
        if analysis.intent is Intent.HUMAN_RECEPTION:
            self._audit_service.record(
                action=AuditAction.HUMAN_TRANSFER,
                actor_type=ActorType.AI_CALLBOT,
                clinic_id=ctx.clinic.id,
                actor_ref=session.session_id,
                metadata={"reason": TransferReason.CALLER_REQUEST.value},
            )
            metrics.increment(M.HUMAN_TRANSFER, labels={"reason": "caller_request"})
            return ctx.transfer(TransferReason.CALLER_REQUEST)

        if analysis.intent is Intent.END_CALL and session.state in (
            ConversationState.BOOKING_COMPLETE,
            ConversationState.CLINIC_INFORMATION,
            ConversationState.IDENTIFY_INTENT,
        ):
            session.state = ConversationState.END_CALL
            return Reply(
                text=(
                    responses.GOODBYE_AFTER_BOOKING
                    if session.booked_appointment_id
                    else responses.GOODBYE
                )
            )

        # Intent switch: a new task intent replaces the current one. Identity
        # already established is kept; task-specific drafts are reset so the
        # new flow does not inherit stale preferences.
        if (
            analysis.intent is not Intent.UNKNOWN
            and analysis.intent is not session.intent
            and analysis.confidence >= 0.8
        ):
            self._switch_intent(session, analysis.intent, analysis.confidence)

        if session.intent is Intent.UNKNOWN:
            # No task intent yet. Before giving up — or assuming a booking —
            # check whether the clinic has an approved answer for this.
            # Matching against approved knowledge rather than adding ever more
            # intent patterns keeps coverage tied to what the clinic signed
            # off on. Questions are checked first: "are you taking new
            # patients?" is a question about the clinic, not a statement that
            # the caller is one.
            # Who works here and what can be booked are answered from the
            # database, so they will never match the knowledge file and would
            # otherwise fall through to "assume they want to book".
            if (analysis.is_question or not analysis.entities.has_any()) and (
                await self._knowledge.lookup(analysis.normalised) is not None
                or directory.asks_about_practitioners(analysis.normalised)
                or directory.asks_about_appointment_types(analysis.normalised)
            ):
                self._switch_intent(session, Intent.CLINIC_INFORMATION, 0.6)
                return None

            if analysis.is_empty:
                return ctx.reprompt(responses.CLARIFY_INTENT, responses.DIDNT_CATCH)
            if not analysis.entities.has_any():
                return ctx.reprompt(responses.CLARIFY_INTENT, responses.DIDNT_UNDERSTAND)

            # Entities but no explicit intent: a caller volunteering a day or a
            # doctor is almost always trying to book.
            self._switch_intent(session, Intent.BOOK_APPOINTMENT, 0.5)

        return None

    def _switch_intent(
        self, session: ConversationSession, intent: Intent, confidence: float
    ) -> None:
        previous = session.intent
        session.intent = intent
        session.intent_confidence = confidence
        session.clear_retries()

        if previous is not Intent.UNKNOWN and previous is not intent:
            # Preferences from the abandoned task must not leak into the new
            # one; identity and verification are deliberately preserved.
            session.booking.clear_offers()
            session.target_appointment_id = None
            session.offered_appointment_ids = []
            session.booking_idempotency_key = None
            logger.info("conversation.intent_switch", previous=previous.value, current=intent.value)

        # Reset to the neutral entry state and let the flow decide where to go.
        # Jumping straight to a flow-internal state here would make the flow
        # believe it had already asked its first question, producing a spurious
        # "Sorry, I didn't catch that" on the caller's opening sentence.
        if session.state not in (
            ConversationState.HUMAN_TRANSFER,
            ConversationState.EMERGENCY,
            ConversationState.END_CALL,
        ):
            session.state = ConversationState.IDENTIFY_INTENT

    async def _dispatch(self, ctx: FlowContext) -> Reply:
        session = ctx.session

        # "Anything else?" — a closed task with no new intent ends the call.
        if session.state is ConversationState.BOOKING_COMPLETE and not (
            ctx.analysis.entities.has_any() or ctx.analysis.affirmative
        ):
            session.state = ConversationState.END_CALL
            return Reply(text=responses.GOODBYE_AFTER_BOOKING)

        handler = {
            Intent.BOOK_APPOINTMENT: booking.handle,
            Intent.RESCHEDULE_APPOINTMENT: reschedule.handle,
            Intent.CANCEL_APPOINTMENT: cancel.handle,
            Intent.CLINIC_INFORMATION: information.handle,
        }.get(session.intent)

        if handler is None:
            return ctx.reprompt(responses.CLARIFY_INTENT, responses.DIDNT_UNDERSTAND)
        return await handler(ctx)

    # --- Memory ------------------------------------------------------------
    @staticmethod
    def _apply_entities(session: ConversationSession, analysis: Analysis) -> set[str]:
        """Fold extracted entities into the session.

        Returns the names of fields whose value actually *changed*, which is
        what makes corrections work: re-stating the same day is not a change,
        but "actually Thursday" is, and only then are offered slots discarded.
        """
        changed: set[str] = set()
        entities = analysis.entities
        draft = session.booking

        if entities.practitioner_id is not None:
            if draft.practitioner_id != entities.practitioner_id:
                changed.add("practitioner")
            draft.practitioner_id = entities.practitioner_id
            draft.practitioner_name = entities.practitioner_name
            draft.practitioner_any = False
        elif entities.practitioner_any and not draft.practitioner_any:
            if draft.practitioner_id is not None:
                changed.add("practitioner")
            draft.practitioner_id = None
            draft.practitioner_name = None
            draft.practitioner_any = True

        if entities.appointment_type_id is not None:
            if draft.appointment_type_id != entities.appointment_type_id:
                changed.add("appointment_type")
            draft.appointment_type_id = entities.appointment_type_id
            draft.appointment_type_name = entities.appointment_type_name

        resolution = entities.date_resolution
        if resolution.day is not None:
            if draft.preferred_date != resolution.day:
                changed.add("preferred_date")
            draft.preferred_date = resolution.day
            draft.date_from = draft.date_to = None
        elif resolution.day_from is not None:
            if draft.date_from != resolution.day_from:
                changed.add("preferred_date")
            draft.preferred_date = None
            draft.date_from = resolution.day_from
            draft.date_to = resolution.day_to

        time_resolution = entities.time_resolution
        if not time_resolution.is_empty:
            if draft.preferred_period != time_resolution.period:
                changed.add("preferred_period")
            draft.preferred_period = time_resolution.period
            if time_resolution.not_before != draft.not_before:
                changed.add("not_before")
            draft.not_before = time_resolution.not_before

        if entities.patient_type is not None and session.patient_type is None:
            session.patient_type = entities.patient_type

        session.booking = draft  # trigger pydantic validation on assignment
        return changed

    # --- Persistence -------------------------------------------------------
    async def _persist(
        self,
        session: ConversationSession,
        message: str,
        reply: Reply,
        *,
        latency_ms: float,
    ) -> None:
        try:
            if session.call_id is not None:
                call = await self._call_service.get_call(session.call_id)
                if call is not None:
                    for event_name, payload in reply.events:
                        self._call_service.record_event(call.id, CallEventType(event_name), payload)
                    self._call_service.record_event(
                        call.id,
                        CallEventType.STATE_CHANGED,
                        {"state": session.state.value, "duration_ms": round(latency_ms)},
                    )
                    self._call_service.record_turn_text(call.id, caller=message, reply=reply.text)
                    if session.is_terminal:
                        await self._call_service.end_call(call, session, outcome_for(session))
                    else:
                        await self._call_service.sync_from_session(call, session)
            await self._db.commit()
        except Exception as exc:
            # Losing call telemetry must not lose the caller's booking or drop
            # the call; the booking itself committed in its own transaction.
            logger.error("conversation.persist_failed", error=type(exc).__name__)
            await self._db.rollback()

        await self._store.save(session)


def outcome_label(session: ConversationSession) -> CallOutcome:
    return outcome_for(session)
