"""Patient lookup.

Anti-enumeration rules enforced here rather than left to callers:

* Lookup requires *two* independent factors (surname **and** date of birth).
  A surname alone never returns a record.
* An ambiguous result (more than one match) is treated exactly like no match.
  Otherwise a caller could learn that two patients share a surname and DOB.
* The caller of this service receives ``None`` for every failure mode, so it
  cannot accidentally produce a distinguishable error message.
* Every lookup is audited, successful or not.
"""

from __future__ import annotations

import re
import uuid
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.service import AuditService
from app.core.logging import get_logger
from app.database.models.enums import ActorType, AuditAction
from app.database.models.patient import Patient

logger = get_logger(__name__)


def normalise_mobile(raw: str | None) -> str | None:
    """Australian mobile numbers to E.164 (+614XXXXXXXX)."""
    if not raw:
        return None
    digits = re.sub(r"[^\d+]", "", raw)
    if digits.startswith("+61"):
        digits = "0" + digits[3:]
    elif digits.startswith("61") and len(digits) == 11:
        digits = "0" + digits[2:]
    if re.fullmatch(r"0[45]\d{8}", digits):
        return "+61" + digits[1:]
    return None


def mask_mobile(mobile: str | None) -> str | None:
    """'+61412345678' -> '••• ••• 678'. Enough to confirm, not to learn."""
    if not mobile or len(mobile) < 4:
        return None
    return f"••• ••• {mobile[-3:]}"


class PatientService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._audit = AuditService(session)

    async def identify(
        self,
        *,
        clinic_id: uuid.UUID,
        last_name: str | None,
        date_of_birth: date | None,
        actor_ref: str | None = None,
    ) -> Patient | None:
        """Find exactly one patient matching surname + date of birth.

        Returns None for: missing factors, no match, or multiple matches. The
        caller must not be able to distinguish between these.
        """
        if not last_name or not date_of_birth:
            return None

        result = await self._session.execute(
            select(Patient)
            .where(
                Patient.clinic_id == clinic_id,
                Patient.active.is_(True),
                func.lower(Patient.last_name) == last_name.strip().lower(),
                Patient.date_of_birth == date_of_birth,
            )
            .limit(2)  # two is enough to detect ambiguity
        )
        matches = list(result.scalars().all())

        found = len(matches) == 1
        self._audit.record(
            action=AuditAction.PATIENT_LOOKUP,
            actor_type=ActorType.AI_CALLBOT,
            actor_ref=actor_ref,
            clinic_id=clinic_id,
            subject_type="patient",
            subject_id=matches[0].id if found else None,
            success=found,
            metadata={"count": len(matches), "reason_code": "surname_dob"},
        )
        if not found:
            logger.info("patient.identify_failed", match_count=len(matches))
            return None
        return matches[0]

    async def create_provisional(
        self,
        *,
        clinic_id: uuid.UUID,
        first_name: str,
        last_name: str,
        date_of_birth: date,
        mobile: str | None,
        actor_ref: str | None = None,
    ) -> Patient:
        """Create a record for a caller who is not yet a patient.

        Details are self-reported over the phone and unverified. Reception is
        expected to confirm and de-duplicate at the first visit; see
        docs/privacy/data-inventory.md.
        """
        patient = Patient(
            clinic_id=clinic_id,
            first_name=first_name.strip().title(),
            last_name=last_name.strip().title(),
            date_of_birth=date_of_birth,
            mobile=mobile,
            mobile_normalised=normalise_mobile(mobile),
        )
        self._session.add(patient)
        await self._session.flush()
        self._audit.record(
            action=AuditAction.PATIENT_LOOKUP,
            actor_type=ActorType.AI_CALLBOT,
            actor_ref=actor_ref,
            clinic_id=clinic_id,
            subject_type="patient",
            subject_id=patient.id,
            metadata={"reason_code": "provisional_create", "source": "AI_CALLBOT"},
        )
        return patient

    async def get(self, patient_id: uuid.UUID) -> Patient | None:
        patient = await self._session.get(Patient, patient_id)
        return patient if patient and patient.active else None
