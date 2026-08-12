"""Safety engine.

Runs on every caller utterance **before** intent detection, entity extraction,
knowledge lookup or any language model. Nothing downstream can override its
verdict: the conversation engine checks the verdict first and, on an emergency,
never reaches the appointment workflow at all.

Design constraints:

* Deterministic. No model, no network, no database. A safety decision must not
  depend on an LLM being up, and must be reproducible in a test.
* Fast. Regex matching over a normalised string; microseconds per turn.
* Extensible. ``SafetyClassifier`` lets a trained classifier be added later as
  an *additional* signal. It can escalate, never de-escalate — a model may
  never talk the engine out of an emergency the rules found.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Protocol, runtime_checkable

from app.core.logging import get_logger
from app.core.metrics import M, metrics
from app.safety.models import SafetyAction, SafetyCategory, SafetyVerdict
from app.safety.rules import NEGATION_GUARDS, SafetyRule, all_rules

logger = get_logger(__name__)

# Contractions and punctuation are collapsed so one pattern covers
# "can't" / "cant" / "can not".
_CONTRACTIONS = re.compile(r"['’]")  # noqa: RUF001 - both apostrophe forms are intended
_NON_TEXT = re.compile(r"[^a-z0-9\s]+")
_WHITESPACE = re.compile(r"\s+")
_CAN_NOT = re.compile(r"\bcan not\b")


def normalise(text: str) -> str:
    """Lowercase, strip accents/punctuation, collapse whitespace.

    Also used by the NLU baseline so that safety and intent see the same view
    of an utterance — a caller cannot dodge a safety rule through phrasing that
    the intent layer still understands.
    """
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = text.lower()
    text = _CONTRACTIONS.sub("", text)  # can't -> cant
    text = _NON_TEXT.sub(" ", text)
    text = _CAN_NOT.sub("cannot", text)
    return _WHITESPACE.sub(" ", text).strip()


@runtime_checkable
class SafetyClassifier(Protocol):
    """Optional additional signal (Milestone 2+).

    Must be fail-open toward safety: if it errors or times out, the engine
    proceeds on the deterministic rules alone.
    """

    async def classify(self, text: str) -> SafetyVerdict | None: ...


class SafetyEngine:
    def __init__(
        self,
        rules: list[SafetyRule] | None = None,
        classifier: SafetyClassifier | None = None,
    ) -> None:
        self._rules = rules if rules is not None else all_rules()
        self._classifier = classifier

    def evaluate(self, text: str) -> SafetyVerdict:
        """Synchronous rule evaluation. This is the authoritative check."""
        if not text or not text.strip():
            return SafetyVerdict()

        normalised = normalise(text)
        # Emergency rules come first in the list, so the first match wins and
        # an emergency can never be masked by a lower-priority escalation.
        for rule in self._rules:
            match = rule.pattern.search(normalised)
            if not match:
                continue
            if self._suppressed(rule, normalised):
                logger.debug("safety.rule_suppressed", rule_id=rule.rule_id)
                continue
            verdict = SafetyVerdict(
                action=rule.action,
                category=rule.category,
                rule_id=rule.rule_id,
                matched_terms=(match.group(0)[:40],),
            )
            self._record(verdict)
            return verdict

        return SafetyVerdict()

    async def evaluate_async(self, text: str) -> SafetyVerdict:
        """Rules plus the optional classifier.

        The classifier may only *raise* severity. A CONTINUE verdict from the
        classifier never overrides an EMERGENCY from the rules.
        """
        verdict = self.evaluate(text)
        if verdict.is_emergency or self._classifier is None:
            return verdict

        try:
            extra = await self._classifier.classify(text)
        except Exception as exc:  # a classifier failure must not break the call
            logger.warning("safety.classifier_failed", error=type(exc).__name__)
            return verdict

        if extra is None or extra.is_clear:
            return verdict
        if _severity(extra.action) > _severity(verdict.action):
            self._record(extra)
            return extra
        return verdict

    def _suppressed(self, rule: SafetyRule, normalised: str) -> bool:
        return any(guard.search(normalised) for guard in NEGATION_GUARDS.get(rule.rule_id, ()))

    @staticmethod
    def _record(verdict: SafetyVerdict) -> None:
        if verdict.is_emergency:
            metrics.increment(M.EMERGENCY_TRIGGERED, labels={"category": verdict.category.value})
            # rule_id only — the utterance itself is never logged.
            logger.warning(
                "safety.emergency", rule_id=verdict.rule_id, category=verdict.category.value
            )
        elif verdict.requires_human:
            metrics.increment(M.SAFETY_ESCALATION, labels={"category": verdict.category.value})
            logger.info(
                "safety.escalation", rule_id=verdict.rule_id, category=verdict.category.value
            )


def _severity(action: SafetyAction) -> int:
    return {
        SafetyAction.CONTINUE: 0,
        SafetyAction.ESCALATE_HUMAN: 1,
        SafetyAction.EMERGENCY: 2,
    }[action]


# --- Approved safety responses ---------------------------------------------
# Spoken verbatim. Wording is fixed here rather than generated, so a model can
# never soften, hedge or add advice to an emergency instruction.


def emergency_response(category: SafetyCategory, emergency_number: str = "000") -> str:
    if category is SafetyCategory.SELF_HARM:
        return (
            f"I'm really sorry you're going through this. Please call triple zero, "
            f"{emergency_number}, or Lifeline on 13 11 14 right now. "
            "They can help you straight away."
        )
    return (
        f"This sounds like an emergency. Please hang up and call triple zero, "
        f"{emergency_number}, now. They can help you immediately."
    )


ESCALATION_RESPONSES: dict[SafetyCategory, str] = {
    SafetyCategory.CLINICAL_ADVICE: (
        "I'm not able to give medical advice. I'll put you through to reception."
    ),
    SafetyCategory.MEDICATION: (
        "I can't help with medication questions. Let me transfer you to reception."
    ),
    SafetyCategory.TEST_RESULTS: (
        "I can't discuss results over this line. I'll put you through to reception."
    ),
    SafetyCategory.DISTRESS: "I'm sorry you're unwell. Let me get you to reception now.",
}


def escalation_response(category: SafetyCategory) -> str:
    return ESCALATION_RESPONSES.get(category, "Let me put you through to reception.")
