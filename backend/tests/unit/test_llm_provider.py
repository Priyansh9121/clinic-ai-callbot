"""Ollama provider: response handling, strict validation and resilience.

All offline — a mock HTTP transport stands in for the runtime, so these run in
CI without Ollama. Live-model checks live in ``tests/llm_live/``.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from app.conversation.states import Intent
from app.llm.circuit import CircuitBreaker
from app.llm.errors import (
    LLMCircuitOpenError,
    LLMInvalidResponseError,
    LLMSaturatedError,
    LLMTimeoutError,
    LLMUnavailableError,
)
from app.llm.ollama import MAX_RESPONSE_BYTES, OllamaLLMProvider
from app.llm.provider import EntityHints, LLMAnalysis

VALID_PAYLOAD = {
    "intent": "BOOK_APPOINTMENT",
    "confidence": 0.94,
    "entities": {
        "practitioner": "Dr Patel",
        "preferred_date_text": "next Friday",
        "preferred_period": "afternoon",
        "appointment_type": "standard GP",
        "patient_type": "existing",
        "selection": None,
    },
    "requires_human": False,
}


def _provider(handler, **kwargs) -> OllamaLLMProvider:
    """Provider wired to a mock transport instead of a live runtime."""
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://test", timeout=5.0
    )
    kwargs.setdefault("base_url", "http://test")
    kwargs.setdefault("model", "test-model")
    return OllamaLLMProvider(client=client, **kwargs)


def _chat_response(content: str | dict, status: int = 200):
    body = content if isinstance(content, str) else json.dumps(content)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"message": {"content": body}})

    if status >= 400:

        def error_handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(status, json={"error": "boom"})

        return error_handler
    return handler


# --- Happy path -------------------------------------------------------------
async def test_valid_response_is_parsed() -> None:
    provider = _provider(_chat_response(VALID_PAYLOAD))
    result = await provider.analyse_message("book me in", state="IDENTIFY_INTENT")

    assert isinstance(result, LLMAnalysis)
    assert result.intent is Intent.BOOK_APPOINTMENT
    assert result.confidence == pytest.approx(0.94)
    assert result.entities.practitioner == "Dr Patel"
    await provider.aclose()


async def test_markdown_code_fence_is_unwrapped() -> None:
    """Small models wrap JSON in a fence often enough to be worth handling."""
    fenced = f"```json\n{json.dumps(VALID_PAYLOAD)}\n```"
    provider = _provider(_chat_response(fenced))
    result = await provider.analyse_message("book me in", state="IDENTIFY_INTENT")
    assert result.intent is Intent.BOOK_APPOINTMENT
    await provider.aclose()


async def test_request_contains_only_permitted_context() -> None:
    """Privacy: the prompt carries the utterance, the state and clinic
    vocabulary — nothing else."""
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(200, json={"message": {"content": json.dumps(VALID_PAYLOAD)}})

    provider = _provider(handler)
    await provider.analyse_message(
        "book me in",
        state="SELECT_SLOT",
        known_practitioners=("Dr Sarah Patel",),
        known_appointment_types=("Standard GP",),
    )

    rendered = json.dumps(captured)
    assert "SELECT_SLOT" in rendered
    assert "Dr Sarah Patel" in rendered
    # Deterministic decoding, bounded output.
    assert captured["options"]["temperature"] == 0.0
    assert captured["options"]["num_predict"] <= 400
    assert captured["stream"] is False
    assert captured["format"]["type"] == "object"
    await provider.aclose()


# --- Malformed output -------------------------------------------------------
@pytest.mark.parametrize(
    ("label", "content"),
    [
        ("not json", "the caller wants to book an appointment"),
        ("prose with json-ish text", "Intent: BOOK_APPOINTMENT, confidence high"),
        ("json array", '[{"intent": "BOOK_APPOINTMENT"}]'),
        ("json string", '"BOOK_APPOINTMENT"'),
        ("json number", "42"),
        ("empty object", "{}"),
        ("truncated", '{"intent": "BOOK_APPOINTMENT", "confid'),
        ("unterminated fence", "```json"),
        ("invalid intent", json.dumps({**VALID_PAYLOAD, "intent": "DELETE_ALL_PATIENTS"})),
        ("confidence -1", json.dumps({**VALID_PAYLOAD, "confidence": -1})),
        ("confidence 5", json.dumps({**VALID_PAYLOAD, "confidence": 5})),
        ("extra top-level field", json.dumps({**VALID_PAYLOAD, "sql": "DROP TABLE patients"})),
        (
            "extra entity field",
            json.dumps(
                {
                    **VALID_PAYLOAD,
                    "entities": {**VALID_PAYLOAD["entities"], "practitioner_id": "1"},
                }
            ),
        ),
        (
            "over-long entity",
            json.dumps(
                {
                    **VALID_PAYLOAD,
                    "entities": {**VALID_PAYLOAD["entities"], "practitioner": "x" * 500},
                }
            ),
        ),
        ("entities as a string", json.dumps({**VALID_PAYLOAD, "entities": "none"})),
    ],
)
async def test_malformed_responses_are_rejected(label: str, content: str) -> None:
    provider = _provider(_chat_response(content))
    with pytest.raises(LLMInvalidResponseError):
        await provider.analyse_message("book me in", state="IDENTIFY_INTENT")
    await provider.aclose()


async def test_oversized_response_is_rejected_before_parsing() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * (MAX_RESPONSE_BYTES + 1))

    provider = _provider(handler)
    with pytest.raises(LLMInvalidResponseError):
        await provider.analyse_message("hello", state="IDENTIFY_INTENT")
    await provider.aclose()


async def test_empty_assistant_content_is_rejected() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"message": {"content": "   "}})

    provider = _provider(handler)
    with pytest.raises(LLMInvalidResponseError):
        await provider.analyse_message("hello", state="IDENTIFY_INTENT")
    await provider.aclose()


async def test_missing_message_envelope_is_rejected() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"unexpected": "shape"})

    provider = _provider(handler)
    with pytest.raises(LLMInvalidResponseError):
        await provider.analyse_message("hello", state="IDENTIFY_INTENT")
    await provider.aclose()


# --- Transport failures -----------------------------------------------------
async def test_connection_refused_raises_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    provider = _provider(handler)
    with pytest.raises(LLMUnavailableError):
        await provider.analyse_message("hello", state="IDENTIFY_INTENT")
    await provider.aclose()


async def test_timeout_raises_timeout_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("too slow")

    provider = _provider(handler)
    with pytest.raises(LLMTimeoutError):
        await provider.analyse_message("hello", state="IDENTIFY_INTENT")
    await provider.aclose()


@pytest.mark.parametrize("status", [400, 404, 500, 503])
async def test_http_errors_raise_unavailable(status: int) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"error": "nope"})

    provider = _provider(handler)
    with pytest.raises(LLMUnavailableError):
        await provider.analyse_message("hello", state="IDENTIFY_INTENT")
    await provider.aclose()


# --- Input bounding ---------------------------------------------------------
async def test_caller_text_is_truncated_before_the_prompt() -> None:
    """A very long utterance must not become a very long model request."""
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(200, json={"message": {"content": json.dumps(VALID_PAYLOAD)}})

    provider = _provider(handler)
    await provider.analyse_message("book " * 5000, state="IDENTIFY_INTENT")

    user_content = captured["messages"][-1]["content"]
    assert len(user_content) < 1000
    await provider.aclose()


# --- Health -----------------------------------------------------------------
async def test_health_true_when_model_is_present() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"models": [{"name": "test-model"}]})

    provider = _provider(handler)
    assert await provider.health() is True
    await provider.aclose()


async def test_health_false_when_model_is_missing() -> None:
    """A running runtime without the configured model is not healthy."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"models": [{"name": "some-other-model"}]})

    provider = _provider(handler)
    assert await provider.health() is False
    await provider.aclose()


async def test_health_false_when_runtime_is_down() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    provider = _provider(handler)
    assert await provider.health() is False
    await provider.aclose()


async def test_health_matches_a_tagged_model_name() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"models": [{"name": "test-model:latest"}]})

    provider = _provider(handler)
    assert await provider.health() is True
    await provider.aclose()


# --- Circuit breaker --------------------------------------------------------
def test_breaker_opens_after_consecutive_failures() -> None:
    breaker = CircuitBreaker(failure_threshold=3, recovery_seconds=60)
    for _ in range(2):
        breaker.record_failure()
    assert breaker.is_open is False
    breaker.record_failure()
    assert breaker.is_open is True


def test_breaker_resets_on_success() -> None:
    breaker = CircuitBreaker(failure_threshold=3)
    breaker.record_failure()
    breaker.record_failure()
    breaker.record_success()
    breaker.record_failure()
    assert breaker.is_open is False


def test_breaker_closes_after_the_cooldown() -> None:
    breaker = CircuitBreaker(failure_threshold=1, recovery_seconds=0.0)
    breaker.record_failure()
    assert breaker.is_open is False  # cooldown already elapsed


async def test_provider_short_circuits_after_repeated_failures() -> None:
    """A dead runtime must stop costing every caller a connection timeout."""
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        raise httpx.ConnectError("refused")

    provider = _provider(handler, failure_threshold=2, recovery_seconds=60)

    for _ in range(2):
        with pytest.raises(LLMUnavailableError):
            await provider.analyse_message("hello", state="IDENTIFY_INTENT")
    assert attempts == 2

    # Third call is refused without touching the network.
    with pytest.raises(LLMCircuitOpenError):
        await provider.analyse_message("hello", state="IDENTIFY_INTENT")
    assert attempts == 2
    assert provider.circuit_open is True
    await provider.aclose()


async def test_malformed_responses_also_open_the_breaker() -> None:
    provider = _provider(_chat_response("not json"), failure_threshold=2, recovery_seconds=60)
    for _ in range(2):
        with pytest.raises(LLMInvalidResponseError):
            await provider.analyse_message("hello", state="IDENTIFY_INTENT")
    with pytest.raises(LLMCircuitOpenError):
        await provider.analyse_message("hello", state="IDENTIFY_INTENT")
    await provider.aclose()


# --- Concurrency ------------------------------------------------------------
async def test_concurrency_is_bounded() -> None:
    """Simultaneous calls must not all hit the model at once."""
    in_flight = 0
    peak = 0

    async def slow_handler(request: httpx.Request) -> httpx.Response:
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0.05)
        in_flight -= 1
        return httpx.Response(200, json={"message": {"content": json.dumps(VALID_PAYLOAD)}})

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(slow_handler), base_url="http://test", timeout=5.0
    )
    provider = OllamaLLMProvider(
        base_url="http://test", model="test-model", max_concurrency=2, client=client
    )

    results = await asyncio.gather(
        *(provider.analyse_message("hello", state="IDENTIFY_INTENT") for _ in range(6)),
        return_exceptions=True,
    )
    assert peak <= 2, f"expected at most 2 concurrent generations, saw {peak}"
    # Excess callers get a clean saturation error rather than queueing forever.
    assert all(isinstance(r, LLMAnalysis | LLMSaturatedError) for r in results), [
        type(r).__name__ for r in results
    ]
    await provider.aclose()


# --- Schema strictness ------------------------------------------------------
def test_entity_hints_reject_unknown_fields() -> None:
    """An invented ``practitioner_id`` must fail the whole response.

    Silently dropping it (the previous ``extra="ignore"``) would let a model
    that had started emitting identifiers look like it was behaving.
    """
    import pydantic

    with pytest.raises(pydantic.ValidationError):
        EntityHints.model_validate({"practitioner": "Dr Patel", "practitioner_id": "abc"})
    with pytest.raises(pydantic.ValidationError):
        EntityHints.model_validate({"slot_id": "00000000-0000-0000-0000-000000000001"})


def test_analysis_rejects_unknown_top_level_fields() -> None:
    import pydantic

    with pytest.raises(pydantic.ValidationError):
        LLMAnalysis.model_validate({**VALID_PAYLOAD, "reply": "Sure, I'll book that."})
    with pytest.raises(pydantic.ValidationError):
        LLMAnalysis.model_validate({**VALID_PAYLOAD, "tool": "book_appointment"})


def test_analysis_rejects_out_of_range_confidence() -> None:
    import pydantic

    for bad in (-0.1, 1.1, 100):
        with pytest.raises(pydantic.ValidationError):
            LLMAnalysis.model_validate({**VALID_PAYLOAD, "confidence": bad})
