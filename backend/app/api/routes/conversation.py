"""Text conversation endpoint.

This is the single entry point to the conversation engine. The terminal
simulator uses it, the tests use it, and the Milestone 3 voice pipeline will
use it too — so there is exactly one implementation of the conversation logic.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, status

from app.api.deps import Engine
from app.schemas.conversation import (
    ConversationMessage,
    ConversationReply,
    ConversationStart,
)

router = APIRouter(prefix="/conversation", tags=["conversation"])


@router.post("/start", response_model=ConversationReply)
async def start_conversation(payload: ConversationStart, engine: Engine) -> ConversationReply:
    result = await engine.start(session_id=payload.session_id, channel=payload.channel)
    return ConversationReply(
        session_id=result.session_id,
        reply=result.reply,
        state=result.state,
        intent=result.intent,
        memory=result.memory,
    )


@router.post("/message", response_model=ConversationReply)
async def post_message(payload: ConversationMessage, engine: Engine) -> ConversationReply:
    result = await engine.handle_message(payload.session_id, payload.message)
    return ConversationReply(
        session_id=result.session_id,
        reply=result.reply,
        state=result.state,
        intent=result.intent,
        requires_human=result.requires_human,
        emergency=result.emergency,
        ended=result.ended,
        transfer_reason=result.transfer_reason,
        latency_ms=result.latency_ms,
        memory=result.memory,
    )


@router.post("/{session_id}/end", status_code=status.HTTP_204_NO_CONTENT)
async def end_conversation(session_id: str, engine: Engine) -> None:
    if len(session_id) > 64:
        raise HTTPException(status_code=422, detail="invalid session id")
    await engine.end(session_id)
