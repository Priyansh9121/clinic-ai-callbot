"""Safety verdict types."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class SafetyCategory(StrEnum):
    """Why safety logic fired. Drives the response and the audit record."""

    NONE = "NONE"

    # Life-threatening — leave the workflow, direct to emergency services.
    MEDICAL_EMERGENCY = "MEDICAL_EMERGENCY"
    SELF_HARM = "SELF_HARM"

    # Out of scope for an automated agent — hand to a human.
    CLINICAL_ADVICE = "CLINICAL_ADVICE"
    MEDICATION = "MEDICATION"
    TEST_RESULTS = "TEST_RESULTS"
    DISTRESS = "DISTRESS"


class SafetyAction(StrEnum):
    CONTINUE = "CONTINUE"
    EMERGENCY = "EMERGENCY"  # highest priority; overrides everything
    ESCALATE_HUMAN = "ESCALATE_HUMAN"


@dataclass(frozen=True, slots=True)
class SafetyVerdict:
    action: SafetyAction = SafetyAction.CONTINUE
    category: SafetyCategory = SafetyCategory.NONE
    # Identifier of the rule that fired, for audit and tuning. Never the text.
    rule_id: str | None = None
    matched_terms: tuple[str, ...] = field(default_factory=tuple)

    @property
    def is_emergency(self) -> bool:
        return self.action is SafetyAction.EMERGENCY

    @property
    def requires_human(self) -> bool:
        return self.action is SafetyAction.ESCALATE_HUMAN

    @property
    def is_clear(self) -> bool:
        return self.action is SafetyAction.CONTINUE
