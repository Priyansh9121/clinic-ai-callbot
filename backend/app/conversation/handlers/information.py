"""Clinic information flow.

Answers come only from approved knowledge entries, verbatim. There is no
generative fallback: an unmatched question goes to reception. This is the
mechanism that stops the callbot inventing a fee, an opening hour or a policy.
"""

from __future__ import annotations

from app.conversation import responses
from app.conversation.handlers.base import FlowContext, Reply
from app.conversation.states import ConversationState, TransferReason
from app.core.logging import get_logger

logger = get_logger(__name__)


async def handle(ctx: FlowContext) -> Reply:
    question = ctx.analysis.normalised

    # Who works here and what can be booked come from the database, not the
    # knowledge file: both change without anyone re-approving wording, and the
    # answer has to agree with what the booking flow offers moments later.
    directory_reply = await _answer_from_directory(ctx, question)
    if directory_reply is not None:
        return directory_reply

    match = await ctx.knowledge.lookup(question)

    if match is None:
        # "No thanks, that's all" after an answer is the caller closing the
        # call, not an unanswerable question — transferring them to reception
        # there would be a poor ending.
        if ctx.session.state is ConversationState.CLINIC_INFORMATION and (
            ctx.analysis.negative or ctx.analysis.is_empty
        ):
            ctx.goto(ConversationState.END_CALL)
            return Reply(text=responses.GOODBYE)
        logger.info("knowledge.miss", state=ctx.session.state.value)
        return ctx.transfer(TransferReason.KNOWLEDGE_GAP)

    entry = match.entry
    if entry.requires_human:
        # The topic is acknowledged with approved wording, then handed over.
        ctx.session.transfer(TransferReason.CLINICAL_QUESTION)
        return Reply(
            text=entry.answer,
            events=[
                (
                    "ESCALATION",
                    {
                        "reason": TransferReason.CLINICAL_QUESTION.value,
                        "category": entry.topic.value,
                    },
                )
            ],
        )

    ctx.goto(ConversationState.CLINIC_INFORMATION)
    ctx.session.clear_retries()
    return Reply(
        text=f"{entry.answer} {responses.ANYTHING_ELSE}",
        events=[("TOOL_INVOKED", {"reason": "knowledge_lookup", "category": entry.topic.value})],
    )


async def _answer_from_directory(ctx: FlowContext, question: str) -> Reply | None:
    """Answer practitioner and appointment-type questions from live rows.

    Returns None when the question is about something else, so the ordinary
    knowledge lookup runs unchanged.
    """
    from app.clinic import directory

    wants_practitioners = directory.asks_about_practitioners(question)
    wants_types = directory.asks_about_appointment_types(question)
    if not (wants_practitioners or wants_types):
        return None

    if wants_practitioners:
        practitioners = await ctx.clinic_service.list_practitioners(ctx.clinic.id)
        if not practitioners:
            # A clinic with no practitioners configured cannot be booked with
            # either. A person is the honest answer, not "we have none".
            return ctx.transfer(TransferReason.KNOWLEDGE_GAP)

        named = directory.find_practitioner_by_name(practitioners, question)
        if named is not None:
            accepting = (
                "and is taking new patients"
                if named.accepts_new_patients
                else "though they are not taking new patients at the moment"
            )
            text = f"Yes, {named.display_name} works here {accepting}."
        else:
            text = directory.describe_practitioners(practitioners)
        category = "practitioners"
    else:
        types = await ctx.clinic_service.list_appointment_types(ctx.clinic.id)
        if not types:
            return ctx.transfer(TransferReason.KNOWLEDGE_GAP)
        text = directory.describe_appointment_types(types)
        category = "appointment_types"

    ctx.goto(ConversationState.CLINIC_INFORMATION)
    ctx.session.clear_retries()
    return Reply(
        text=f"{text} {responses.ANYTHING_ELSE}",
        events=[("TOOL_INVOKED", {"reason": "directory_lookup", "category": category})],
    )
