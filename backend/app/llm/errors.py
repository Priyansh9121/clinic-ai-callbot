"""LLM provider failure taxonomy.

Distinct types because each is a different operational signal — a timeout means
the model is too slow for a phone call, an invalid response means the model or
prompt is wrong — and because the metrics label is derived from the type. All
of them produce the same caller-visible behaviour: fall back to the
deterministic analyser and say nothing about it.
"""

from __future__ import annotations

from app.core.errors import ProviderResponseError, ProviderUnavailableError


class LLMUnavailableError(ProviderUnavailableError):
    """The runtime is not reachable, or refused the connection."""

    code = "llm_unavailable"


class LLMTimeoutError(ProviderUnavailableError):
    """The model did not answer inside ``LLM_TIMEOUT_SECONDS``."""

    code = "llm_timeout"


class LLMCircuitOpenError(ProviderUnavailableError):
    """Skipped without calling: recent consecutive failures opened the breaker."""

    code = "llm_circuit_open"


class LLMSaturatedError(ProviderUnavailableError):
    """Concurrency limit reached and the wait budget elapsed."""

    code = "llm_saturated"


class LLMInvalidResponseError(ProviderResponseError):
    """The model replied, but not with something that validates.

    Covers malformed JSON, a JSON array, extra or missing fields, an unknown
    intent, an out-of-range confidence, an over-long field, and an oversized
    response body.
    """

    code = "llm_invalid_response"


#: Metrics label for each failure. Closed set — safe as a label value.
FAILURE_REASONS: dict[type[Exception], str] = {
    LLMUnavailableError: "unavailable",
    LLMTimeoutError: "timeout",
    LLMCircuitOpenError: "circuit_open",
    LLMSaturatedError: "saturated",
    LLMInvalidResponseError: "invalid_response",
}


def failure_reason(exc: BaseException) -> str:
    for exc_type, reason in FAILURE_REASONS.items():
        if isinstance(exc, exc_type):
            return reason
    return "error"
