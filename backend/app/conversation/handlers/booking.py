"""Booking flow.

Structured as slot filling rather than a fixed script: each turn fills whatever
the caller volunteered, then asks for the highest-priority field still missing.
"I need to see Dr Patel on Friday afternoon" fills three fields at once and
skips three questions.

Corrections fall out of the same design. When the engine reports that a
preference changed, the flow discards the offers already read out and searches
again — it does not restart identification or re-ask settled questions.
"""

from __future__ import annotations

import uuid

from app.appointments.availability import AvailabilityQuery
from app.appointments.service import BookingRequest
from app.conversation import responses
from app.conversation.handlers.base import (
    FlowContext,
    Reply,
    describe_slot,
    describe_slot_options,
    select_from_offers,
)
from app.conversation.handlers.identity import ensure_identified
from app.conversation.states import ConversationState, PatientType, TransferReason
from app.core.errors import (
    BookingRuleError,
    DuplicateBookingError,
    SlotUnavailableError,
)
from app.core.logging import get_logger
from app.core.metrics import M, metrics
from app.core.timeutils import DayPeriod, day_bounds, speak_datetime
from app.database.models.availability import AvailabilitySlot
from app.database.models.enums import BookingSource

logger = get_logger(__name__)

#: Preference fields that invalidate slots already offered.
PREFERENCE_FIELDS = frozenset(
    {"preferred_date", "preferred_period", "practitioner", "appointment_type", "not_before"}
)


async def handle(ctx: FlowContext) -> Reply:
    session = ctx.session
    preferences_changed = bool(ctx.changed & PREFERENCE_FIELDS)

    offered_now: list[AvailabilitySlot] | None = None
    if preferences_changed and session.booking.offered_slot_ids:
        # Repeating one of the times just read out is how most callers choose:
        # "12 pm", not "the second one". But a bare time also reads as a period
        # preference — 12 pm is an afternoon — and that lands in
        # PREFERENCE_FIELDS. Treated as a correction it throws the offers away
        # and searches again, so the caller is re-offered the very slot they
        # asked for and can never leave this state.
        #
        # What settles it is whether the utterance actually matches something
        # already on the table, which only the offers can answer. A time that
        # matches is a choice; one that does not ("actually, make it Friday")
        # is the correction this branch exists for.
        offered_now = await _load_offered(ctx)
        if (
            session.state is ConversationState.SELECT_SLOT
            and offered_now
            and select_from_offers(ctx.analysis, offered_now, ctx.tz) is not None
        ):
            preferences_changed = False
        else:
            session.booking.clear_offers()
            offered_now = None
            logger.info("booking.preferences_changed", fields=sorted(ctx.changed))

    if not preferences_changed:
        if session.state is ConversationState.CONFIRM_BOOKING:
            decided = await _handle_confirmation(ctx)
            if decided is not None:
                return decided
        elif session.state is ConversationState.SELECT_SLOT:
            decided = await _handle_selection(ctx, offered=offered_now)
            if decided is not None:
                return decided

    identity_reply = await ensure_identified(ctx)
    if identity_reply is not None:
        return identity_reply

    type_reply = await _ensure_appointment_type(ctx)
    if type_reply is not None:
        return type_reply

    practitioner_reply = await _ensure_practitioner(ctx)
    if practitioner_reply is not None:
        return practitioner_reply

    date_reply = await _ensure_date(ctx)
    if date_reply is not None:
        return date_reply

    return await _search_and_offer(ctx)


# --- Field gathering --------------------------------------------------------
async def _ensure_appointment_type(ctx: FlowContext) -> Reply | None:
    session = ctx.session
    draft = session.booking
    if draft.appointment_type_id is not None:
        return None

    types = await ctx.clinic_service.list_appointment_types(ctx.clinic.id)
    if not types:
        return ctx.transfer(TransferReason.TECHNICAL_FAILURE)

    # A new patient always needs the new-patient appointment type; asking them
    # to choose would be meaningless.
    if session.patient_type is PatientType.NEW:
        chosen = next(
            (t for t in types if t.slug == "new_patient"),
            next((t for t in types if t.new_patient_allowed), types[0]),
        )
        draft.appointment_type_id = chosen.id
        draft.appointment_type_name = chosen.name
        return None

    if session.state is not ConversationState.APPOINTMENT_TYPE:
        ctx.goto(ConversationState.APPOINTMENT_TYPE)
        return Reply(text=responses.ASK_APPOINTMENT_TYPE)

    # Asked once and still unresolved — default to the shortest standard type
    # rather than looping. Reception can adjust it; a stuck caller cannot.
    chosen = next((t for t in types if t.slug == "standard_gp"), types[0])
    draft.appointment_type_id = chosen.id
    draft.appointment_type_name = chosen.name
    session.clear_retries()
    logger.info("booking.appointment_type_defaulted", slug=chosen.slug)
    return None


async def _ensure_practitioner(ctx: FlowContext) -> Reply | None:
    session = ctx.session
    draft = session.booking
    if draft.practitioner_id is not None or draft.practitioner_any:
        return None

    if session.state is not ConversationState.PRACTITIONER:
        ctx.goto(ConversationState.PRACTITIONER)
        return Reply(text=responses.ASK_PRACTITIONER)

    # No clear preference after asking: treat as no preference.
    draft.practitioner_any = True
    session.clear_retries()
    return None


async def _ensure_date(ctx: FlowContext) -> Reply | None:
    session = ctx.session
    draft = session.booking

    if not draft.has_date_preference:
        if session.state is ConversationState.PREFERRED_DATE:
            return ctx.reprompt(responses.ASK_DATE, responses.DIDNT_CATCH)
        ctx.goto(ConversationState.PREFERRED_DATE)
        return Reply(text=responses.ASK_DATE)

    # A period question is only worth a turn when the day is busy enough that
    # three offers would not represent it.
    if (
        draft.preferred_date is not None
        and draft.preferred_period is DayPeriod.ANY
        and draft.not_before is None
        and session.state is not ConversationState.PREFERRED_PERIOD
    ):
        count = len(await _search(ctx, limit=ctx.settings.max_slot_offers * 3))
        if count > ctx.settings.max_slot_offers * 2:
            ctx.goto(ConversationState.PREFERRED_PERIOD)
            return Reply(text=responses.ASK_PERIOD)

    session.clear_retries()
    return None


# --- Availability -----------------------------------------------------------
def _build_query(ctx: FlowContext, *, limit: int) -> AvailabilityQuery:
    draft = ctx.session.booking
    date_from = date_to = None
    if draft.preferred_date is None and draft.date_from is not None:
        date_from, _ = day_bounds(draft.date_from, ctx.tz)
        _, date_to = day_bounds(draft.date_to or draft.date_from, ctx.tz)
    return AvailabilityQuery(
        clinic_id=ctx.clinic.id,
        appointment_type_id=draft.appointment_type_id,
        practitioner_id=draft.practitioner_id,
        day=draft.preferred_date,
        period=draft.preferred_period,
        date_from=date_from,
        date_to=date_to,
        limit=limit,
    )


async def _search(ctx: FlowContext, *, limit: int) -> list[AvailabilitySlot]:
    return await ctx.availability_service.search(_build_query(ctx, limit=limit), ctx.tz)


async def _search_and_offer(ctx: FlowContext) -> Reply:
    session = ctx.session
    ctx.goto(ConversationState.SEARCHING_AVAILABILITY)

    slots, exact = await ctx.availability_service.search_with_fallback(
        _build_query(ctx, limit=40), ctx.tz
    )
    if not slots:
        return await _handle_no_availability(ctx)

    offered = _spread(slots, ctx.settings.max_slot_offers)
    session.booking.offered_slot_ids = [s.id for s in offered]

    if len(offered) == 1:
        # A single option doubles as the confirmation question.
        session.booking.selected_slot_id = offered[0].id
        ctx.goto(ConversationState.CONFIRM_BOOKING)
        text = responses.OFFER_SINGLE.format(
            slot=describe_slot(offered[0], ctx.tz, ctx.today, with_practitioner=True)
        )
        if not exact:
            text = f"Nothing at that exact time. {text}"
        return Reply(
            text=text,
            events=[("TOOL_INVOKED", {"count": 1, "reason": "availability_search"})],
        )

    ctx.goto(ConversationState.SELECT_SLOT)
    options = describe_slot_options(offered, ctx.tz, ctx.today)
    text = (
        responses.OFFER_MULTIPLE.format(slots=options)
        if exact
        else responses.OFFER_ALTERNATIVE.format(requested=_requested_phrase(ctx), slots=options)
    )
    return Reply(
        text=text,
        events=[("TOOL_INVOKED", {"count": len(offered), "reason": "availability_search"})],
    )


async def _handle_no_availability(ctx: FlowContext) -> Reply:
    session = ctx.session
    # Distinguish "not that day" from "nothing at all": only the latter is a
    # reason to give up and transfer.
    anywhere = await ctx.availability_service.search(
        AvailabilityQuery(
            clinic_id=ctx.clinic.id,
            appointment_type_id=session.booking.appointment_type_id,
            limit=1,
        ),
        ctx.tz,
    )
    if not anywhere:
        return ctx.transfer(TransferReason.NO_AVAILABILITY)

    if session.booking.practitioner_id is not None:
        # The constraint is probably the named practitioner.
        session.booking.practitioner_id = None
        session.booking.practitioner_name = None
        session.booking.practitioner_any = True
        session.booking.clear_offers()
        return await _search_and_offer(ctx)

    session.booking.preferred_date = None
    session.booking.date_from = None
    session.booking.date_to = None
    session.booking.preferred_period = DayPeriod.ANY
    session.booking.not_before = None
    ctx.goto(ConversationState.PREFERRED_DATE)
    return Reply(text=responses.NO_AVAILABILITY)


def _spread(slots: list[AvailabilitySlot], count: int) -> list[AvailabilitySlot]:
    """Pick evenly spaced options rather than the first N consecutive ones.

    Offering "9:00, 9:15 or 9:30" gives a caller no real choice; spreading the
    picks across what is available does.
    """
    if len(slots) <= count:
        return slots
    step = (len(slots) - 1) / (count - 1) if count > 1 else 0
    return [slots[round(i * step)] for i in range(count)]


def _requested_phrase(ctx: FlowContext) -> str:
    from app.core.timeutils import speak_date

    draft = ctx.session.booking
    parts: list[str] = []
    if draft.preferred_date:
        parts.append(f"on {speak_date(draft.preferred_date, ctx.tz, today=ctx.today)}")
    if draft.preferred_period is not DayPeriod.ANY:
        parts.append(f"in the {draft.preferred_period.value.lower()}")
    return " ".join(parts) if parts else "then"


# --- Selection and confirmation --------------------------------------------
async def _handle_selection(
    ctx: FlowContext, *, offered: list[AvailabilitySlot] | None = None
) -> Reply | None:
    session = ctx.session
    # Reuse the list the caller-intent check already loaded: re-reading it here
    # would be a second round of database reads on every turn, and a caller is
    # waiting on the far end of each one.
    offered = offered if offered is not None else await _load_offered(ctx)
    if not offered:
        session.booking.clear_offers()
        return None  # fall through and search again

    if ctx.analysis.negative:
        session.booking.clear_offers()
        ctx.goto(ConversationState.PREFERRED_DATE)
        return Reply(text=responses.NO_AVAILABILITY)

    chosen = select_from_offers(ctx.analysis, offered, ctx.tz)
    if chosen is None:
        return ctx.reprompt(
            responses.OFFER_MULTIPLE.format(
                slots=describe_slot_options(offered, ctx.tz, ctx.today)
            ),
            responses.DIDNT_CATCH,
        )

    session.booking.selected_slot_id = chosen.id
    session.clear_retries()
    ctx.goto(ConversationState.CONFIRM_BOOKING)
    return Reply(
        text=responses.CONFIRM_BOOKING.format(
            practitioner=chosen.practitioner.display_name,
            when=speak_datetime(chosen.start_time, ctx.tz, today=ctx.today),
        )
    )


async def _handle_confirmation(ctx: FlowContext) -> Reply | None:
    session = ctx.session
    if ctx.analysis.affirmative:
        return await _execute_booking(ctx)

    if ctx.analysis.negative:
        session.booking.clear_offers()
        ctx.goto(ConversationState.PREFERRED_DATE)
        return Reply(text=responses.ASK_DATE)

    # Neither yes nor no, and no preference changed — re-ask rather than book.
    slot_id = session.booking.selected_slot_id
    if slot_id is None:
        return None
    slot = await ctx.availability_service.get_slot(slot_id)
    if slot is None:
        session.booking.clear_offers()
        return None
    return ctx.reprompt(
        responses.CONFIRM_BOOKING.format(
            practitioner=slot.practitioner.display_name,
            when=speak_datetime(slot.start_time, ctx.tz, today=ctx.today),
        ),
        responses.DIDNT_CATCH,
    )


async def _execute_booking(ctx: FlowContext) -> Reply:
    """Commit the booking. Nothing is said to the caller until it is durable."""
    session = ctx.session
    slot_id = session.booking.selected_slot_id
    if slot_id is None:
        session.booking.clear_offers()
        return await _search_and_offer(ctx)

    patient_id = await _resolve_patient_id(ctx)
    if patient_id is None:
        return ctx.transfer(TransferReason.VERIFICATION_FAILED)

    if session.booking_idempotency_key is None:
        # Stable per (call, slot): a retried turn cannot double-book, but a
        # deliberate second booking in the same call still can.
        session.booking_idempotency_key = f"{session.session_id}:{slot_id}"

    try:
        result = await ctx.appointment_service.book(
            BookingRequest(
                clinic_id=ctx.clinic.id,
                patient_id=patient_id,
                slot_id=slot_id,
                source=BookingSource.AI_CALLBOT,
                idempotency_key=session.booking_idempotency_key,
                is_new_patient=session.patient_type is PatientType.NEW,
                actor_ref=session.session_id,
            )
        )
    except SlotUnavailableError:
        # Lost the race between offering and confirming.
        session.booking_idempotency_key = None
        session.booking.clear_offers()
        metrics.increment(M.BOOKING_FAILURE, labels={"reason": "slot_taken"})
        retry = await _search_and_offer(ctx)
        return Reply(
            text=f"{responses.BOOKING_FAILED_TAKEN} {retry.text}",
            events=[*retry.events, ("BOOKING_FAILED", {"reason": "slot_taken"})],
        )
    except DuplicateBookingError as exc:
        session.booking_idempotency_key = None
        ctx.goto(ConversationState.BOOKING_COMPLETE)
        return Reply(
            text=f"{exc.public_message} {responses.ANYTHING_ELSE}",
            events=[("BOOKING_FAILED", {"reason": "duplicate"})],
        )
    except BookingRuleError as exc:
        session.booking_idempotency_key = None
        session.booking.clear_offers()
        ctx.goto(ConversationState.PREFERRED_DATE)
        return Reply(
            text=f"{exc.public_message} {responses.ASK_DATE}",
            events=[("BOOKING_FAILED", {"reason": "policy"})],
        )
    except Exception as exc:
        # Unexpected failure: hand the caller to a human, and keep the full
        # traceback so the cause is diagnosable rather than just "technical
        # problem".
        logger.error("booking.failed", error=type(exc).__name__, exc_info=True)
        return ctx.transfer(TransferReason.TECHNICAL_FAILURE)

    appointment = result.appointment
    session.booked_appointment_id = appointment.id
    session.booking.clear_offers()
    ctx.goto(ConversationState.BOOKING_COMPLETE)
    return Reply(
        text=responses.BOOKING_CONFIRMED.format(
            practitioner=appointment.practitioner.display_name,
            when=speak_datetime(appointment.start_time, ctx.tz, today=ctx.today),
        ),
        events=[
            (
                "BOOKING_CONFIRMED",
                {
                    "appointment_id": str(appointment.id),
                    "idempotent_replay": result.replayed,
                },
            )
        ],
    )


async def _resolve_patient_id(ctx: FlowContext) -> uuid.UUID | None:
    """Return the patient to book for, creating a record only if needed.

    A provisional record for a new patient is created here — at the moment of
    booking — so an abandoned conversation never leaves a patient row behind.
    """
    session = ctx.session
    if session.patient_id is not None:
        return session.patient_id

    draft = session.new_patient
    if session.patient_type is PatientType.NEW and draft.is_complete:
        patient = await ctx.patient_service.create_provisional(
            clinic_id=ctx.clinic.id,
            first_name=draft.first_name,  # type: ignore[arg-type]
            last_name=draft.last_name,  # type: ignore[arg-type]
            date_of_birth=draft.date_of_birth,  # type: ignore[arg-type]
            mobile=draft.mobile,
            actor_ref=session.session_id,
        )
        session.patient_id = patient.id
        return patient.id
    return None


async def _load_offered(ctx: FlowContext) -> list[AvailabilitySlot]:
    """Re-read the offered slots, dropping any taken since they were spoken."""
    from app.database.models.enums import SlotStatus

    slots: list[AvailabilitySlot] = []
    for slot_id in ctx.session.booking.offered_slot_ids:
        slot = await ctx.availability_service.get_slot(slot_id)
        if slot is not None and slot.status is SlotStatus.AVAILABLE:
            slots.append(slot)
    return slots
