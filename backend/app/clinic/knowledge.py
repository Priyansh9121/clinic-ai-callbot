"""Approved clinic knowledge.

The callbot may only state clinic facts that appear in an approved knowledge
entry. There is no generative fallback: if no entry matches the caller's
question, the engine offers reception. This is what stops a language model
inventing a bulk-billing policy or an opening hour.

The provider is an interface so a retrieval-backed implementation can replace
the static one later without touching conversation logic. Any future
implementation must keep the same contract: **return an approved answer or
nothing.** It may never synthesise text.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol, runtime_checkable


class KnowledgeTopic(StrEnum):
    OPENING_HOURS = "OPENING_HOURS"
    LOCATION = "LOCATION"
    PARKING = "PARKING"
    ACCESSIBILITY = "ACCESSIBILITY"
    PRACTITIONERS = "PRACTITIONERS"
    SERVICES = "SERVICES"
    APPOINTMENT_TYPES = "APPOINTMENT_TYPES"
    FEES = "FEES"
    BULK_BILLING = "BULK_BILLING"
    CANCELLATION_POLICY = "CANCELLATION_POLICY"
    TELEHEALTH = "TELEHEALTH"
    NEW_PATIENTS = "NEW_PATIENTS"
    REFERRALS = "REFERRALS"
    REPEAT_PRESCRIPTIONS = "REPEAT_PRESCRIPTIONS"
    TEST_RESULTS = "TEST_RESULTS"
    AFTER_HOURS = "AFTER_HOURS"


@dataclass(frozen=True, slots=True)
class KnowledgeEntry:
    """One approved answer.

    ``answer`` is spoken verbatim, so it is written to be short and voice-first.
    ``requires_human`` marks topics the bot may acknowledge but must not handle
    (test results, prescriptions) — it reads the answer, then transfers.
    """

    topic: KnowledgeTopic
    answer: str
    keywords: tuple[str, ...] = ()
    requires_human: bool = False


@dataclass(frozen=True, slots=True)
class KnowledgeMatch:
    entry: KnowledgeEntry
    score: float


@runtime_checkable
class ClinicKnowledgeProvider(Protocol):
    """Interface for approved-knowledge lookup."""

    async def lookup(self, question: str) -> KnowledgeMatch | None:
        """Return the best approved answer, or None when nothing is approved."""
        ...

    async def topic(self, topic: KnowledgeTopic) -> KnowledgeEntry | None: ...


@dataclass
class StaticClinicKnowledge:
    """Keyword-scored lookup over a fixed, human-approved entry list.

    Scoring is intentionally simple and explainable: a match requires a keyword
    to appear as a whole word, and the best score wins. Below ``min_score``
    nothing is returned, which routes the caller to reception rather than to a
    guess.
    """

    entries: list[KnowledgeEntry] = field(default_factory=list)
    min_score: float = 1.0

    def __post_init__(self) -> None:
        self._by_topic = {e.topic: e for e in self.entries}
        # Pre-compile whole-word patterns; keyword lookup runs on every turn.
        self._patterns: list[tuple[KnowledgeEntry, list[re.Pattern[str]]]] = [
            (e, [re.compile(rf"\b{re.escape(k)}\b", re.IGNORECASE) for k in e.keywords])
            for e in self.entries
        ]

    async def lookup(self, question: str) -> KnowledgeMatch | None:
        best: KnowledgeMatch | None = None
        for entry, patterns in self._patterns:
            score = 0.0
            for pattern, keyword in zip(patterns, entry.keywords, strict=True):
                if pattern.search(question):
                    # Longer keywords are more specific. Scoring on characters
                    # rather than words stops a generic multi-word phrase
                    # outranking a precise single term — "do you do telehealth"
                    # must resolve to telehealth, not to the services entry.
                    score += 1.0 + len(keyword) / 10.0
            if score >= self.min_score and (best is None or score > best.score):
                best = KnowledgeMatch(entry=entry, score=score)
        return best

    async def topic(self, topic: KnowledgeTopic) -> KnowledgeEntry | None:
        return self._by_topic.get(topic)
