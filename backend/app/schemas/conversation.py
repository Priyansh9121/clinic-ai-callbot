"""Conversation API schemas.

``POST /api/conversation/message`` is the seam the voice pipeline will use in
Milestone 3: STT produces the ``message``, TTS speaks the ``reply``. Keeping
the contract text-only means the conversation engine is fully testable without
any audio stack.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, field_validator

from app.conversation.states import ConversationState, Intent

#: Utterances are bounded. A phone turn is short; anything longer is either a
#: client bug or an attempt to overload downstream processing.
MAX_MESSAGE_LENGTH = 1000


class ConversationStart(BaseModel):
    session_id: str | None = Field(default=None, min_length=6, max_length=64)
    channel: str = Field(default="text", pattern=r"^(text|voice|simulator)$")


class ConversationMessage(BaseModel):
    session_id: str = Field(min_length=6, max_length=64, pattern=r"^[A-Za-z0-9_:.-]+$")
    message: str = Field(min_length=1, max_length=MAX_MESSAGE_LENGTH)

    @field_validator("message")
    @classmethod
    def _clean(cls, value: str) -> str:
        # Strip control characters; caller text is data, never markup or
        # instructions, and nothing downstream interprets it as either.
        cleaned = "".join(ch for ch in value if ch.isprintable() or ch in " \t")
        cleaned = cleaned.strip()
        if not cleaned:
            raise ValueError("message must contain readable text")
        return cleaned


class ConversationReply(BaseModel):
    session_id: str
    reply: str
    state: ConversationState
    intent: Intent
    requires_human: bool = False
    emergency: bool = False
    ended: bool = False
    transfer_reason: str | None = None
    latency_ms: float | None = None
    #: Structured conversation memory. Ids and flags only — no patient details.
    memory: dict[str, Any] | None = None
