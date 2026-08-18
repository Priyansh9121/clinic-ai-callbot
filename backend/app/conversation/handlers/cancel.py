"""Cancellation flow.

Two safeguards worth noting:

* The cancellation is confirmed back to the caller *before* it is executed, so
  a misheard "cancel" cannot silently remove a booking.
* ``expected_patient_id`` is passed to the service, so even a correct
  appointment id belonging to someone else is rejected.
"""

from __future__ import annotations

from app.conversation import responses
from app.conversation.handlers.base import FlowContext, Reply, describe_appointment
from app.conversation.handlers.identity import ensure_identified
from app.conversation.states import ConversationState, PatientType, TransferReason
from app.core.errors import AppointmentStateError
from app.core.logging import get_logger
from app.core.timeutils import speak_datetime
from app.database.models.appointment import Appointment

logger = get_logger(__name__)


async def handle(ctx: FlowContext) -> Reply:
    session = ctx.session

    if session.patient_type is None:
        session.patient_type = PatientType.EXISTING
    if session.patient_type is PatientType.NEW:
        return ctx.transfer(TransferReason.UNSUPPORTED_REQUEST)

    identity_reply = await ensure_identified(ctx)
    if identity_reply is not None:
        return identity_reply

    if session.state is ConversationState.CANCEL_CONFIRMATION:
        decided = await _handle_confirmation(ctx)
        if decided is not None:
            return decided

    return await _select_appointment(ctx)


async def _select_appointment(ctx: FlowContext) -> Reply:
    session = ctx.session
    assert session.patient_id is not None

    upcoming = await ctx.appointment_service.upcoming_for_patient(session.patient_id)
    if not upcoming:
        # Same wording whether the caller has no bookings or was not matched.
        return ctx.transfer(TransferReason.UNSUPPORTED_REQUEST)

    chosen = upcoming[0] if len(upcoming) == 1 else _match_appointment(ctx, upcoming)
    if chosen is None:
        session.offered_appointment_ids = [a.id for a in upcoming]
        ctx.goto(ConversationState.CANCEL_LOOKUP)
        return Reply(
            text=responses.ASK_WHICH_APPOINTMENT.format(
                count=len(upcoming),
                options=responses.join_options(
                    [describe_appointment(a, ctx.tz, ctx.today) for a in upcoming]
                ),
            )
        )

    session.target_appointment_id = chosen.id
    ctx.goto(ConversationState.CANCEL_CONFIRMATION)
    return Reply(
        text=responses.CONFIRM_CANCEL.format(
            when=speak_datetime(chosen.start_time, ctx.tz, today=ctx.today),
            practitioner=chosen.practitioner.display_name,
        )
    )


def _match_appointment(ctx: FlowContext, upcoming: list[Appointment]) -> Appointment | None:
    entities = ctx.analysis.entities
    if entities.preferred_date is not None:
        matches = [
            a for a in upcoming if a.start_time.astimezone(ctx.tz).date() == entities.preferred_date
        ]
        if len(matches) == 1:
            return matches[0]
    ordinal = entities.selection_ordinal
    if ordinal is not None:
        if ordinal == -1:
            return upcoming[-1]
        if 1 <= ordinal <= len(upcoming):
            return upcoming[ordinal - 1]
    return None


async def _handle_confirmation(ctx: FlowContext) -> Reply | None:
    session = ctx.session
    if ctx.analysis.negative:
        session.target_appointment_id = None
        ctx.goto(ConversationState.BOOKING_COMPLETE)
        return Reply(text=responses.CANCEL_ABORTED)
    if not ctx.analysis.affirmative:
        return None
    if session.target_appointment_id is None:
        return None
    if session.patient_id is None:
        # Identity is established before this state is reachable, but that is a
        # property of the flow rather than of the type. If it ever stops being
        # true, refusing is the only safe answer: the alternative is a mutation
        # with no owner, which is exactly what `owner` exists to prevent.
        return ctx.transfer(TransferReason.VERIFICATION_FAILED)

    try:
        await ctx.appointment_service.cancel(
            session.target_appointment_id,
            reason="caller request via AI callbot",
            actor_ref=session.session_id,
            owner=session.patient_id,
        )
    except AppointmentStateError as exc:
        ctx.goto(ConversationState.BOOKING_COMPLETE)
        return Reply(text=f"{exc.public_message} {responses.ANYTHING_ELSE}")
    except Exception as exc:
        logger.error("cancel.failed", error=type(exc).__name__)
        return ctx.transfer(TransferReason.TECHNICAL_FAILURE)

    cancelled_id = session.target_appointment_id
    session.target_appointment_id = None
    ctx.goto(ConversationState.BOOKING_COMPLETE)
    return Reply(
        text=responses.CANCEL_CONFIRMED,
        events=[("TOOL_INVOKED", {"reason": "cancel", "appointment_id": str(cancelled_id)})],
    )
