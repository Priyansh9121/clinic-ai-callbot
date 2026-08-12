"""Domain error hierarchy.

Errors carry a stable machine-readable ``code`` plus a ``public_message`` that
is safe to return to an untrusted caller. Internal detail stays in ``detail``
and is logged, never serialised to an API response.
"""

from __future__ import annotations

from typing import Any


class AppError(Exception):
    """Base class for all expected application errors."""

    code: str = "internal_error"
    http_status: int = 500
    public_message: str = "Something went wrong. Please try again."

    def __init__(
        self,
        detail: str | None = None,
        *,
        public_message: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> None:
        self.detail = detail or self.public_message
        if public_message:
            self.public_message = public_message
        self.context = context or {}
        super().__init__(self.detail)


# --- Generic ---------------------------------------------------------------
class NotFoundError(AppError):
    code = "not_found"
    http_status = 404
    public_message = "The requested resource was not found."


class ValidationError(AppError):
    code = "validation_error"
    http_status = 422
    public_message = "The request was not valid."


class ConflictError(AppError):
    code = "conflict"
    http_status = 409
    public_message = "The request conflicts with the current state."


class RateLimitError(AppError):
    code = "rate_limited"
    http_status = 429
    public_message = "Too many requests. Please try again shortly."


class AuthenticationError(AppError):
    code = "unauthenticated"
    http_status = 401
    public_message = "Authentication required."


class AuthorizationError(AppError):
    code = "forbidden"
    http_status = 403
    public_message = "You do not have permission to perform this action."


# --- Booking ---------------------------------------------------------------
class SlotUnavailableError(ConflictError):
    """The chosen slot was taken (or is not bookable) at commit time."""

    code = "slot_unavailable"
    public_message = "That time is no longer available."


class DuplicateBookingError(ConflictError):
    code = "duplicate_booking"
    public_message = "An appointment already exists for that time."


class BookingRuleError(ValidationError):
    """A clinic policy rule rejected the booking (lead time, referral, etc.)."""

    code = "booking_rule_violation"
    public_message = "That appointment cannot be booked as requested."


class AppointmentStateError(ConflictError):
    code = "appointment_state"
    public_message = "That appointment can no longer be changed."


# --- Conversation ----------------------------------------------------------
class SessionNotFoundError(NotFoundError):
    code = "session_not_found"
    public_message = "That conversation session has expired."


# --- Providers (LLM / STT / TTS / telephony) -------------------------------
class ProviderUnavailableError(AppError):
    """An external local engine is not configured or not reachable.

    Callers must degrade deterministically rather than propagating this to the
    caller on the phone.
    """

    code = "provider_unavailable"
    http_status = 503
    public_message = "That service is temporarily unavailable."


class ProviderResponseError(AppError):
    """A provider replied, but the reply failed validation."""

    code = "provider_response_invalid"
    http_status = 502
    public_message = "That service returned an unusable response."
