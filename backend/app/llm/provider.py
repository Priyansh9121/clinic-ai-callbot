"""Local LLM provider interface.

Implemented by ``app.llm.ollama.OllamaLLMProvider``. ``LLM_PROVIDER`` defaults
to ``none``, in which case the system runs entirely on the deterministic
baseline analyser and behaves exactly as it did in Milestone 1.

The interface constrains what a model is ever allowed to do, and that
constraint is what the architecture is built around:

* A provider returns **structured analysis only** — an intent, a confidence and
  entity *hints*. It returns no SQL, no tool name, no patient id, and no text
  that is spoken to a caller without passing through the response catalogue.
* Entity hints are strings. Resolving a hint to a database id happens in
  ``app.conversation.vocabulary`` and ``app.conversation.nlu.dates``, against
  real records and a real clock. A hallucinated doctor or an impossible date
  therefore resolves to nothing rather than to a booking.
* Every response is validated with Pydantic. On validation failure, timeout or
  low confidence, the caller falls back to the deterministic analyser. A model
  being wrong, slow or down degrades quality; it never causes an action.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from app.conversation.states import Intent


class EntityHints(BaseModel):
    """Unresolved entity *text* as the model understood it.

    Every field is free text on purpose: these are hints to be resolved and
    validated by application code, never values to act on directly.

    ``extra="forbid"`` matches the outer schema. It was ``ignore`` while this
    was an unused interface, which would have silently dropped an invented
    ``practitioner_id`` rather than rejecting the whole response. Silent
    dropping is the more dangerous default: it makes a model that has started
    emitting identifiers look like it is behaving. Now the response is refused
    and the turn falls back to the deterministic analyser.
    """

    model_config = ConfigDict(extra="forbid")

    practitioner: str | None = Field(default=None, max_length=80)
    preferred_date_text: str | None = Field(default=None, max_length=60)
    preferred_period: str | None = Field(default=None, max_length=20)
    appointment_type: str | None = Field(default=None, max_length=60)
    patient_type: str | None = Field(default=None, max_length=20)
    selection: str | None = Field(default=None, max_length=30)


class LLMAnalysis(BaseModel):
    """The complete contract for what a model may return.

    ``extra="forbid"`` is deliberate: a response containing anything outside
    this schema is rejected rather than partially trusted.
    """

    model_config = ConfigDict(extra="forbid")

    intent: Intent
    confidence: float = Field(ge=0.0, le=1.0)
    entities: EntityHints = Field(default_factory=EntityHints)
    requires_human: bool = False


@runtime_checkable
class LLMProvider(Protocol):
    """Interface for a local model runtime (llama.cpp, Ollama, …).

    Implementations must be self-hosted. They must also be cancellable and
    bounded by ``LLM_TIMEOUT_SECONDS``: a slow model must not hold a caller in
    silence.
    """

    async def analyse_message(
        self,
        message: str,
        *,
        state: str,
        known_practitioners: tuple[str, ...] = (),
        known_appointment_types: tuple[str, ...] = (),
    ) -> LLMAnalysis:
        """Classify one caller utterance.

        ``state`` and the vocabulary tuples are the *only* context supplied —
        never a transcript, and never patient data. That keeps prompts small
        (latency) and keeps personal information out of the model (privacy).
        """
        ...

    async def health(self) -> bool:
        """Whether the runtime is reachable. Used by ``GET /health``."""
        ...
