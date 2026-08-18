"""Reschedule flow.

Reads an existing appointment, so it requires the caller to be identified
first. The move itself is a single atomic operation in
``AppointmentService.reschedule`` — the caller is never left without their
original appointment because the replacement slot was taken.
"""

from __future__ import annotations

from app.appointments.availability import AvailabilityQuery
from app.conversation import responses
from app.conversation.handlers.base import (
    FlowContext,
    Reply,
    describe_appointment,
    describe_slot_options,
    select_from_offers,
)
from app.conversation.handlers.booking import _spread
from app.conversation.handlers.identity import ensure_identified
from app.conversation.states import ConversationState, PatientType, TransferReason
from app.core.errors import AppointmentStateError, SlotUnavailableError
from app.core.logging import get_logger
from app.core.timeutils import DayPeriod, day_bounds, speak_datetime
from app.database.models.appointment import Appointment

logger = get_logger(__name__)

PREFERENCE_FIELDS = frozenset({"preferred_date", "preferred_period", "not_before"})

logger = get_logger(__name__)


async def handle(ctx: FlowContext) -> Reply:
    session = ctx.session

    # Only an existing patient can have something to move.
    if session.patient_type is None:
        session.patient_type = PatientType.EXISTING
    if session.patient_type is PatientType.NEW:
        return ctx.transfer(TransferReason.UNSUPPORTED_REQUEST)

    identity_reply = await ensure_identified(ctx)
    if identity_reply is not None:
        return identity_reply

    if session.state is ConversationState.RESCHEDULE_CONFIRMATION and not (
        ctx.changed & PREFERENCE_FIELDS
    ):
        decided = await _handle_confirmation(ctx)
        if decided is not None:
            return decided

    if session.target_appointment_id is None:
        lookup_reply = await _select_appointment(ctx)
        if lookup_reply is not None:
            return lookup_reply

    if session.state is ConversationState.RESCHEDULE_SELECTION and (
        session.booking.offered_slot_ids and not (ctx.changed & PREFERENCE_FIELDS)
    ):
        chosen = await _handle_slot_selection(ctx)
        if chosen is not None:
            return chosen

    if not session.booking.has_date_preference:
        ctx.goto(ConversationState.RESCHEDULE_SELECTION)
        return Reply(text=responses.ASK_NEW_TIME)

    return await _offer_new_times(ctx)


async def _select_appointment(ctx: FlowContext) -> Reply | None:
    """Find which appointment the caller means. None once it is settled."""
    session = ctx.session
    assert session.patient_id is not None

    upcoming = await ctx.appointment_service.upcoming_for_patient(session.patient_id)
    if not upcoming:
        return ctx.transfer(TransferReason.UNSUPPORTED_REQUEST)

    if len(upcoming) == 1:
        session.target_appointment_id = upcoming[0].id
        ctx.goto(ConversationState.RESCHEDULE_SELECTION)
        if session.booking.has_date_preference:
            return None
        return Reply(
            text=(
                responses.CONFIRM_APPOINTMENT_FOUND.format(
                    when=speak_datetime(upcoming[0].start_time, ctx.tz, today=ctx.today),
                    practitioner=upcoming[0].practitioner.display_name,
                )
                + " "
                + responses.ASK_NEW_TIME
            )
        )

    chosen = _match_appointment(ctx, upcoming)
    if chosen is not None:
        session.target_appointment_id = chosen.id
        session.offered_appointment_ids = []
        ctx.goto(ConversationState.RESCHEDULE_SELECTION)
        return None

    session.offered_appointment_ids = [a.id for a in upcoming]
    ctx.goto(ConversationState.RESCHEDULE_LOOKUP)
    return Reply(
        text=responses.ASK_WHICH_APPOINTMENT.format(
            count=len(upcoming),
            options=responses.join_options(
                [describe_appointment(a, ctx.tz, ctx.today) for a in upcoming]
            ),
        )
    )


def _match_appointment(ctx: FlowContext, upcoming: list[Appointment]) -> Appointment | None:
    """Pick the appointment the caller referred to, if unambiguous."""
    entities = ctx.analysis.entities

    # "the one on Friday" — but only when the date names an existing booking,
    # not when it is the *new* time they want.
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

    if ctx.analysis.affirmative and len(upcoming) == 1:
        return upcoming[0]
    return None


async def _offer_new_times(ctx: FlowContext) -> Reply:
    session = ctx.session
    original = await ctx.appointment_service.get(session.target_appointment_id)  # type: ignore[arg-type]

    draft = session.booking
    date_from = date_to = None
    if draft.preferred_date is None and draft.date_from is not None:
        date_from, _ = day_bounds(draft.date_from, ctx.tz)
        _, date_to = day_bounds(draft.date_to or draft.date_from, ctx.tz)

    slots, exact = await ctx.availability_service.search_with_fallback(
        AvailabilityQuery(
            clinic_id=ctx.clinic.id,
            # Keep the same appointment type and practitioner unless the
            # caller says otherwise — they are moving, not rebooking.
            appointment_type_id=original.appointment_type_id,
            practitioner_id=(
                draft.practitioner_id
                if draft.practitioner_id
                else (None if draft.practitioner_any else original.practitioner_id)
            ),
            day=draft.preferred_date,
            period=draft.preferred_period,
            date_from=date_from,
            date_to=date_to,
            limit=40,
        ),
        ctx.tz,
    )
    if not slots:
        draft.preferred_date = None
        draft.date_from = None
        draft.preferred_period = DayPeriod.ANY
        ctx.goto(ConversationState.RESCHEDULE_SELECTION)
        return Reply(text=responses.NO_AVAILABILITY)

    offered = _spread(slots, ctx.settings.max_slot_offers)
    draft.offered_slot_ids = [s.id for s in offered]

    if len(offered) == 1:
        draft.selected_slot_id = offered[0].id
        ctx.goto(ConversationState.RESCHEDULE_CONFIRMATION)
        return Reply(
            text=responses.CONFIRM_RESCHEDULE.format(
                when=speak_datetime(offered[0].start_time, ctx.tz, today=ctx.today)
            )
        )

    ctx.goto(ConversationState.RESCHEDULE_SELECTION)
    options = describe_slot_options(offered, ctx.tz, ctx.today)
    return Reply(
        text=(
            responses.OFFER_MULTIPLE.format(slots=options)
            if exact
            else responses.OFFER_ALTERNATIVE.format(requested="then", slots=options)
        )
    )


async def _handle_slot_selection(ctx: FlowContext) -> Reply | None:
    session = ctx.session
    offered = []
    for slot_id in session.booking.offered_slot_ids:
        slot = await ctx.availability_service.get_slot(slot_id)
        if slot is not None and slot.is_bookable:
            offered.append(slot)
    if not offered:
        session.booking.clear_offers()
        return None

    chosen = select_from_offers(ctx.analysis, offered, ctx.tz)
    if chosen is None:
        if ctx.analysis.negative:
            session.booking.clear_offers()
            session.booking.preferred_date = None
            session.booking.date_from = None
            return Reply(text=responses.ASK_NEW_TIME)
        return ctx.reprompt(
            responses.OFFER_MULTIPLE.format(
                slots=describe_slot_options(offered, ctx.tz, ctx.today)
            ),
            responses.DIDNT_CATCH,
        )

    session.booking.selected_slot_id = chosen.id
    session.clear_retries()
    ctx.goto(ConversationState.RESCHEDULE_CONFIRMATION)
    return Reply(
        text=responses.CONFIRM_RESCHEDULE.format(
            when=speak_datetime(chosen.start_time, ctx.tz, today=ctx.today)
        )
    )


async def _handle_confirmation(ctx: FlowContext) -> Reply | None:
    session = ctx.session
    if ctx.analysis.negative:
        session.booking.clear_offers()
        session.booking.preferred_date = None
        session.booking.date_from = None
        ctx.goto(ConversationState.RESCHEDULE_SELECTION)
        return Reply(text=responses.ASK_NEW_TIME)
    if not ctx.analysis.affirmative:
        return None

    slot_id = session.booking.selected_slot_id
    if slot_id is None or session.target_appointment_id is None:
        return None

    if session.patient_id is None:
        # Identity is established before this state is reachable, but that is a
        # property of the flow rather than of the type. If it ever stops being
        # true, refusing is the only safe answer: the alternative is a mutation
        # with no owner, which is exactly what `owner` exists to prevent.
        return ctx.transfer(TransferReason.VERIFICATION_FAILED)

    if session.booking_idempotency_key is None:
        session.booking_idempotency_key = f"{session.session_id}:resched:{slot_id}"

    try:
        moved = await ctx.appointment_service.reschedule(
            session.target_appointment_id,
            slot_id,
            actor_ref=session.session_id,
            owner=session.patient_id,
            idempotency_key=session.booking_idempotency_key,
        )
    except SlotUnavailableError:
        session.booking_idempotency_key = None
        session.booking.clear_offers()
        retry = await _offer_new_times(ctx)
        return Reply(text=f"{responses.BOOKING_FAILED_TAKEN} {retry.text}")
    except AppointmentStateError as exc:
        return Reply(text=f"{exc.public_message} {responses.ANYTHING_ELSE}")
    except Exception as exc:
        logger.error("reschedule.failed", error=type(exc).__name__)
        return ctx.transfer(TransferReason.TECHNICAL_FAILURE)

    session.target_appointment_id = moved.id
    session.booked_appointment_id = moved.id
    session.booking.clear_offers()
    ctx.goto(ConversationState.BOOKING_COMPLETE)
    return Reply(
        text=responses.RESCHEDULE_CONFIRMED.format(
            when=speak_datetime(moved.start_time, ctx.tz, today=ctx.today)
        ),
        events=[("BOOKING_CONFIRMED", {"appointment_id": str(moved.id), "reason": "reschedule"})],
    )
