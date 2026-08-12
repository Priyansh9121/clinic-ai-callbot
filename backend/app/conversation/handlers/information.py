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
