"""Test doubles for the LLM provider.

Ordinary ``pytest`` must never need Ollama running, so every unit,
conversation and security test uses these. The live-model checks are marked
``llm_live`` and skipped by default.
"""

from __future__ import annotations

import asyncio
from typing import Any

from app.conversation.states import Intent
from app.llm.errors import (
    LLMCircuitOpenError,
    LLMInvalidResponseError,
    LLMSaturatedError,
    LLMTimeoutError,
    LLMUnavailableError,
)
from app.llm.provider import EntityHints, LLMAnalysis


class FakeLLMProvider:
    """Returns scripted results, or raises a scripted failure.

    ``responses`` may hold ``LLMAnalysis`` objects or exceptions; each call
    consumes the next, and the last one repeats once exhausted.
    """

    def __init__(
        self,
        responses: list[LLMAnalysis | Exception] | None = None,
        *,
        healthy: bool = True,
        delay_seconds: float = 0.0,
    ) -> None:
        self._responses: list[Any] = list(responses or [])
        self._healthy = healthy
        self._delay = delay_seconds
        self.calls: list[dict[str, Any]] = []

    async def analyse_message(
        self,
        message: str,
        *,
        state: str,
        known_practitioners: tuple[str, ...] = (),
        known_appointment_types: tuple[str, ...] = (),
    ) -> LLMAnalysis:
        # Recorded so tests can assert exactly what was sent to the model —
        # in particular that no patient data ever is.
        self.calls.append(
            {
                "message": message,
                "state": state,
                "known_practitioners": known_practitioners,
                "known_appointment_types": known_appointment_types,
            }
        )
        if self._delay:
            await asyncio.sleep(self._delay)
        if not self._responses:
            raise LLMUnavailableError(detail="no scripted response")
        response = self._responses[0] if len(self._responses) == 1 else self._responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    async def health(self) -> bool:
        return self._healthy

    @property
    def call_count(self) -> int:
        return len(self.calls)


def analysis(
    intent: Intent = Intent.BOOK_APPOINTMENT,
    confidence: float = 0.95,
    *,
    requires_human: bool = False,
    **hints: str | None,
) -> LLMAnalysis:
    """Build a valid ``LLMAnalysis`` for tests."""
    return LLMAnalysis(
        intent=intent,
        confidence=confidence,
        entities=EntityHints(**hints),
        requires_human=requires_human,
    )


#: Every failure the analyser must absorb, as (label, exception).
PROVIDER_FAILURES: list[tuple[str, Exception]] = [
    ("unavailable", LLMUnavailableError(detail="connection refused")),
    ("timeout", LLMTimeoutError(detail="no response in time")),
    ("invalid_response", LLMInvalidResponseError(detail="schema validation failed")),
    ("circuit_open", LLMCircuitOpenError(detail="breaker open")),
    ("saturated", LLMSaturatedError(detail="concurrency limit")),
    ("unexpected", RuntimeError("something entirely unexpected")),
]
