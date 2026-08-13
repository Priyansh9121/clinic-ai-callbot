"""Structured logging with mandatory redaction of sensitive values.

Privacy requirement: application logs must never carry patient identifiers,
free-text caller utterances, OTP codes or credentials. Redaction is implemented
as a structlog processor so it applies to *every* log record regardless of the
call site — a developer cannot forget to redact.
"""

from __future__ import annotations

import logging
import re
import sys
from typing import Any

import structlog

from app.core.config import Settings

# Keys whose values are replaced wholesale. Matched case-insensitively against
# the *end* of the key so `patient_mobile` and `mobile` both redact.
SENSITIVE_KEY_SUFFIXES: tuple[str, ...] = (
    "password",
    "secret",
    "token",
    "api_key",
    "apikey",
    "authorization",
    "otp",
    "otp_code",
    "date_of_birth",
    "dob",
    "mobile",
    "phone",
    "email",
    "first_name",
    "last_name",
    "full_name",
    "address",
    "medicare",
    "transcript",
    "utterance",
    "message_text",
    "caller_text",
    "reply_text",
)

REDACTED = "[redacted]"

# Free-text that slips through as a value still gets pattern-scrubbed.
_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\b(?:\+?61|0)[2-478](?:[ -]?\d){8}\b"), "[phone]"),
    (re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.]+\b"), "[email]"),
    (re.compile(r"\b\d{4}[-/]\d{2}[-/]\d{2}\b"), "[date]"),
    (re.compile(r"\b\d{10}\b"), "[number]"),
)


def scrub_text(value: str) -> str:
    """Best-effort scrub of identifiers embedded in a free-text string."""
    for pattern, replacement in _PATTERNS:
        value = pattern.sub(replacement, value)
    return value


def _is_sensitive(key: str) -> bool:
    lowered = key.lower()
    return any(lowered == s or lowered.endswith("_" + s) for s in SENSITIVE_KEY_SUFFIXES)


def redact_processor(
    _logger: Any, _method: str, event_dict: structlog.types.EventDict
) -> structlog.types.EventDict:
    """structlog processor: drop sensitive keys, scrub remaining free text."""
    for key, value in list(event_dict.items()):
        if _is_sensitive(key):
            event_dict[key] = REDACTED
        elif isinstance(value, str):
            event_dict[key] = scrub_text(value)
        elif isinstance(value, dict):
            event_dict[key] = {k: (REDACTED if _is_sensitive(k) else v) for k, v in value.items()}
    return event_dict


def configure_logging(settings: Settings) -> None:
    """Configure structlog + stdlib logging. Safe to call more than once."""
    level = getattr(logging, settings.log_level.upper(), logging.INFO)

    shared: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        redact_processor,  # must run last before rendering
    ]
    renderer: structlog.types.Processor = (
        structlog.processors.JSONRenderer()
        if settings.log_format == "json"
        else structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty())
    )

    structlog.configure(
        processors=[*shared, structlog.processors.format_exc_info, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        cache_logger_on_first_use=True,
    )
    logging.basicConfig(level=level, stream=sys.stderr, format="%(message)s", force=True)
    # uvicorn's access log duplicates our structured request log and can echo
    # query strings; keep it quiet.
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    # httpx logs a line per outbound request. It carries no caller text, but it
    # is one line per conversation turn once the LLM is enabled, and it is not
    # routed through our redaction processor — so it stays off by default.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    # Bound explicitly rather than via ``add_logger_name``, which requires a
    # stdlib logger object; we render through structlog's PrintLogger.
    return structlog.get_logger().bind(logger=name)  # type: ignore[no-any-return]
