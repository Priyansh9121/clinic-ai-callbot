"""Ollama provider — a local model runtime speaking the ``LLMProvider`` contract.

Ollama-specific knowledge is confined to this module. Nothing in the
conversation layer knows the runtime exists; swapping in llama.cpp means adding
a sibling module and one line in the factory.

What this class does: build a small prompt, call the local runtime with a JSON
schema, enforce a timeout and a size bound, validate strictly, return an
``LLMAnalysis``.

What it must never do: resolve database entities, touch session state, call a
domain service, decide whether a booking succeeded, or produce caller-facing
text.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
from pydantic import ValidationError

from app.core.logging import get_logger
from app.llm.circuit import CircuitBreaker
from app.llm.errors import (
    LLMCircuitOpenError,
    LLMInvalidResponseError,
    LLMSaturatedError,
    LLMTimeoutError,
    LLMUnavailableError,
)
from app.llm.prompt import SYSTEM_PROMPT, build_user_prompt
from app.llm.provider import LLMAnalysis

logger = get_logger(__name__)

#: Hard ceiling on the response body. Model output is untrusted input, and a
#: runaway generation must not be parsed at all.
MAX_RESPONSE_BYTES = 16_384

#: Caller text is truncated before it reaches the prompt. A phone turn is
#: short; anything longer is a client bug or an attempt to inflate the request.
MAX_PROMPT_MESSAGE_CHARS = 400

#: How long to wait for a concurrency slot before giving up and letting the
#: caller fall back. Waiting longer than this defeats the point of the bound.
SEMAPHORE_WAIT_SECONDS = 0.5

#: Sent to Ollama as `format`, so the runtime constrains decoding to this
#: shape rather than us hoping for well-formed JSON. Validation still runs
#: afterwards — this reduces the invalid-response rate, it does not replace
#: the check.
RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "intent": {"type": "string"},
        "confidence": {"type": "number"},
        "entities": {
            "type": "object",
            "properties": {
                "practitioner": {"type": ["string", "null"]},
                "preferred_date_text": {"type": ["string", "null"]},
                "preferred_period": {"type": ["string", "null"]},
                "appointment_type": {"type": ["string", "null"]},
                "patient_type": {"type": ["string", "null"]},
                "selection": {"type": ["string", "null"]},
            },
            "required": [
                "practitioner",
                "preferred_date_text",
                "preferred_period",
                "appointment_type",
                "patient_type",
                "selection",
            ],
        },
        "requires_human": {"type": "boolean"},
    },
    "required": ["intent", "confidence", "entities", "requires_human"],
}


class OllamaLLMProvider:
    """Concrete ``LLMProvider`` backed by a local Ollama runtime."""

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        timeout_seconds: float = 3.0,
        max_output_tokens: int = 200,
        temperature: float = 0.0,
        max_concurrency: int = 2,
        failure_threshold: int = 3,
        recovery_seconds: float = 30.0,
        keep_alive: str = "30m",
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._timeout = timeout_seconds
        # Ollama unloads an idle model (5 minutes by default) and the next
        # caller then pays a multi-second reload — which on this hardware
        # exceeded the request timeout, failed three turns in a row and opened
        # the circuit breaker. Keeping the model resident is what makes warm
        # latency the latency callers actually experience.
        self._keep_alive = keep_alive
        self._max_output_tokens = max_output_tokens
        self._temperature = temperature
        # One client for the process: connection reuse matters when every
        # caller turn may make a request.
        self._client = client or httpx.AsyncClient(
            base_url=self._base_url,
            timeout=httpx.Timeout(timeout_seconds, connect=min(1.0, timeout_seconds)),
            limits=httpx.Limits(
                max_connections=max_concurrency + 2, max_keepalive_connections=max_concurrency
            ),
        )
        self._owns_client = client is None
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._breaker = CircuitBreaker(
            failure_threshold=failure_threshold, recovery_seconds=recovery_seconds
        )

    @property
    def model(self) -> str:
        return self._model

    @property
    def name(self) -> str:
        return "ollama"

    @property
    def circuit_open(self) -> bool:
        return self._breaker.is_open

    # --- LLMProvider -------------------------------------------------------
    async def analyse_message(
        self,
        message: str,
        *,
        state: str,
        known_practitioners: tuple[str, ...] = (),
        known_appointment_types: tuple[str, ...] = (),
    ) -> LLMAnalysis:
        if self._breaker.is_open:
            raise LLMCircuitOpenError(detail="circuit open after consecutive failures")

        prompt = build_user_prompt(
            message[:MAX_PROMPT_MESSAGE_CHARS],
            state=state,
            known_practitioners=known_practitioners,
            known_appointment_types=known_appointment_types,
        )

        try:
            # Bounded wait: if every slot is busy, fall back rather than making
            # this caller queue behind other callers' generations.
            await asyncio.wait_for(self._semaphore.acquire(), timeout=SEMAPHORE_WAIT_SECONDS)
        except TimeoutError as exc:
            raise LLMSaturatedError(detail="concurrency limit reached") from exc

        try:
            payload = await self._post_chat(prompt)
        except Exception:
            self._breaker.record_failure()
            raise
        finally:
            self._semaphore.release()

        try:
            analysis = self._parse(payload)
        except Exception:
            # A malformed response is a real failure and counts toward the
            # breaker: a model that cannot produce valid JSON will not start
            # doing so on the next turn.
            self._breaker.record_failure()
            raise

        self._breaker.record_success()
        return analysis

    async def health(self) -> bool:
        """Whether the runtime is up *and* the configured model is present.

        A running Ollama without the model pulled would otherwise report
        healthy and then fail on every turn.
        """
        try:
            response = await self._client.get("/api/tags", timeout=2.0)
            response.raise_for_status()
            models = response.json().get("models", [])
            names = {m.get("name", "") for m in models if isinstance(m, dict)}
        except Exception as exc:
            logger.warning("llm.health_failed", provider="ollama", error=type(exc).__name__)
            return False
        # Ollama reports "qwen2.5:1.5b-instruct"; a configured bare name should
        # still match its ":latest" form.
        return any(n == self._model or n.split(":")[0] == self._model for n in names)

    async def warmup(self) -> bool:
        """Load the model into memory with a synthetic utterance.

        The first generation after a cold start costs seconds while weights are
        loaded. Doing that here means a caller does not pay for it. The input
        is fixed synthetic text — never anything from a real call.
        """
        try:
            await self.analyse_message("I need an appointment tomorrow", state="IDENTIFY_INTENT")
        except Exception as exc:
            logger.info("llm.warmup_failed", provider="ollama", error=type(exc).__name__)
            return False
        logger.info("llm.warmup_ok", provider="ollama", model=self._model)
        return True

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    # --- Internals ---------------------------------------------------------
    async def _post_chat(self, prompt: str) -> str:
        """Call the runtime and return the raw assistant text."""
        request = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            "stream": False,
            "format": RESPONSE_SCHEMA,
            "keep_alive": self._keep_alive,
            "options": {
                "temperature": self._temperature,
                "num_predict": self._max_output_tokens,
                # Deterministic decoding: the same utterance should classify
                # the same way twice, or the tests are meaningless.
                "top_k": 1,
                "seed": 0,
            },
        }
        try:
            response = await self._client.post("/api/chat", json=request)
        except httpx.TimeoutException as exc:
            raise LLMTimeoutError(detail=f"no response within {self._timeout}s") from exc
        except httpx.HTTPError as exc:
            raise LLMUnavailableError(detail=type(exc).__name__) from exc

        if response.status_code >= 400:
            raise LLMUnavailableError(detail=f"http {response.status_code}")

        body = response.content
        if len(body) > MAX_RESPONSE_BYTES:
            raise LLMInvalidResponseError(detail=f"response too large ({len(body)} bytes)")

        try:
            envelope = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise LLMInvalidResponseError(detail="runtime envelope was not JSON") from exc

        if not isinstance(envelope, dict):
            raise LLMInvalidResponseError(detail="runtime envelope was not an object")
        content = envelope.get("message", {}).get("content")
        if not isinstance(content, str) or not content.strip():
            raise LLMInvalidResponseError(detail="runtime returned no assistant content")
        return content

    @staticmethod
    def _parse(content: str) -> LLMAnalysis:
        """Decode and strictly validate the assistant text.

        Tolerates a Markdown code fence, because small models wrap JSON in one
        often enough to matter and unwrapping it is unambiguous. Everything
        else is rejected — no regex scraping of prose, no field coercion, no
        "best effort". An unparseable response is a fallback, not a puzzle.
        """
        text = content.strip()
        if text.startswith("```"):
            fenced = text.split("```")
            if len(fenced) < 2:
                raise LLMInvalidResponseError(detail="unterminated code fence")
            body = fenced[1]
            if body.startswith("json"):
                body = body[4:]
            text = body.strip()

        try:
            data = json.loads(text)
        except (json.JSONDecodeError, ValueError) as exc:
            raise LLMInvalidResponseError(detail="assistant content was not JSON") from exc

        if not isinstance(data, dict):
            # A JSON array, string or number is not the contract.
            raise LLMInvalidResponseError(
                detail=f"expected a JSON object, got {type(data).__name__}"
            )

        try:
            return LLMAnalysis.model_validate(data)
        except ValidationError as exc:
            # Only the error *count* is logged. The response may echo caller
            # text, so neither the payload nor the messages are recorded.
            raise LLMInvalidResponseError(
                detail=f"schema validation failed ({exc.error_count()} errors)"
            ) from exc
