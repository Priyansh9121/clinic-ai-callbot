"""Audit trail writer.

The service takes an ``AsyncSession`` and *stages* the audit row in the caller's
transaction. That is deliberate: an audit record for a booking must commit with
the booking, and must roll back if the booking rolls back, so the trail can
never claim something happened that did not.

``metadata`` must stay non-identifying. ``_SAFE_KEYS`` is enforced rather than
documented, because an audit table is exactly where a well-meaning change would
otherwise start accumulating patient detail.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.core.timeutils import utc_now
from app.database.models.audit import AuditEvent
from app.database.models.enums import ActorType, AuditAction

logger = get_logger(__name__)

# Keys permitted in audit metadata. Anything else is dropped and counted.
_SAFE_KEYS: frozenset[str] = frozenset(
    {
        "reason",
        "reason_code",
        "source",
        "channel",
        "state",
        "intent",
        "outcome",
        "slot_id",
        "appointment_id",
        "previous_appointment_id",
        "practitioner_id",
        "location_id",
        "appointment_type_id",
        "start_time",
        "attempt",
        "attempts_remaining",
        "duration_ms",
        "confidence",
        "rule_id",
        "category",
        "count",
        "idempotent_replay",
        "error_code",
        "role",
    }
)


def _sanitise(metadata: dict[str, Any] | None) -> dict[str, Any]:
    if not metadata:
        return {}
    clean: dict[str, Any] = {}
    dropped = 0
    for key, value in metadata.items():
        if key not in _SAFE_KEYS:
            dropped += 1
            continue
        # uuid/datetime are not JSON-serialisable for JSONB via asyncpg.
        clean[key] = str(value) if isinstance(value, uuid.UUID) else value
    if dropped:
        clean["_dropped_keys"] = dropped
    return clean


class AuditService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def record(
        self,
        *,
        action: AuditAction,
        actor_type: ActorType,
        clinic_id: uuid.UUID | None = None,
        actor_ref: str | None = None,
        subject_type: str | None = None,
        subject_id: uuid.UUID | None = None,
        success: bool = True,
        metadata: dict[str, Any] | None = None,
    ) -> AuditEvent:
        """Stage an audit row in the caller's transaction (no commit here)."""
        event = AuditEvent(
            clinic_id=clinic_id,
            occurred_at=utc_now(),
            action=action,
            actor_type=actor_type,
            actor_ref=actor_ref,
            subject_type=subject_type,
            subject_id=subject_id,
            success=success,
            metadata_json=_sanitise(metadata),
        )
        self._session.add(event)
        logger.info(
            "audit",
            action=action.value,
            actor_type=actor_type.value,
            subject_type=subject_type,
            success=success,
        )
        return event
