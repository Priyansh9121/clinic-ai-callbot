"""LLM-backed ``MessageAnalyser``.

Implements the same protocol as ``BaselineAnalyser``, so ``ConversationEngine``
is unchanged and unaware that a model exists. It composes rather than replaces:

    LLMBackedAnalyser
       ├── BaselineAnalyser   always runs — cheap, and the fallback
       └── LLMProvider        consulted only when it can add something

Three properties this is built to guarantee:

1. **Never worse than the baseline.** The baseline ``Analysis`` is the starting
   point, and model hints only *fill fields it left empty*. A hint can add
   understanding the deterministic parser missed; it can never overrule
   something the parser did resolve.
2. **Never blocking.** Any provider failure — down, slow, malformed, saturated,
   low confidence — returns the baseline result for that same turn. The caller
   is never told the AI failed; quality just degrades.
3. **Never authoritative over deterministic decisions.** An explicit request
   for a human, a clear yes/no, or an empty utterance is answered by the
   baseline without consulting the model at all.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import replace
from datetime import date

from app.conversation.nlu.baseline import Analysis, BaselineAnalyser, Vocabulary
from app.conversation.nlu.resolution import resolve_hints
from app.conversation.states import IDENTIFICATION_STATES, Intent
from app.core.logging import get_logger
from app.core.metrics import M, metrics
from app.llm.errors import failure_reason
from app.llm.provider import LLMAnalysis, LLMProvider

logger = get_logger(__name__)

#: Baseline confidence at or above which the model is not consulted: an
#: explicit request for a human (0.95) and a clear reschedule/cancel (0.9).
#:
#: Deliberately above the booking pattern's 0.85. That pattern is the loosest
#: in the baseline and over-matches — it reads "I don't think I'll need that
#: booking" as a *booking* — so letting it skip the model locked in an error
#: the model would have corrected. Precise patterns skip; loose ones ask.
HYBRID_SKIP_CONFIDENCE = 0.9

#: States where a bare fragment is ambiguous enough that the model earns its
#: latency even when the baseline found something.
_ALWAYS_CONSULT_STATES = frozenset(
    {"SELECT_SLOT", "RESCHEDULE_SELECTION", "RESCHEDULE_LOOKUP", "CANCEL_LOOKUP"}
)

#: States where the caller may legitimately be starting a new task, so a task
#: intent from the model is meaningful. Everywhere else the agent has asked a
#: question and the utterance is an *answer* to it.
_INTENT_OPEN_STATES = frozenset(
    {
        "GREETING",
        "AI_DISCLOSURE",
        "IDENTIFY_INTENT",
        "BOOKING_COMPLETE",  # "anything else?"
        "CLINIC_INFORMATION",  # ditto, after an answer
    }
)

#: The two intents a caller can always express, mid-answer or not.
_ALWAYS_ALLOWED_INTENTS = frozenset({Intent.HUMAN_RECEPTION, Intent.END_CALL})

#: While the caller is giving their identity, the model is not consulted at all.
#:
#: Two reasons, one of them a bug found in live testing. ``EntityHints`` has no
#: identity fields, so the model cannot contribute anything useful here. But it
#: *can* do harm: asked to classify "14 March 1958", it returned
#: ``preferred_date_text="14 March 1958"``, which resolved forward to 14 March
#: 2027 and became the appointment date — a date with no availability, so the
#: booking silently died three turns later. The caller's date of birth is not
#: an appointment preference.
#:
#: The deterministic analyser already restricts extraction to identity fields
#: in these states; this applies the same rule to the model path.
_IDENTITY_STATES = frozenset({s.value for s in IDENTIFICATION_STATES})


class LLMBackedAnalyser:
    """``MessageAnalyser`` that consults a local model, with deterministic fallback."""

    def __init__(
        self,
        *,
        provider: LLMProvider,
        baseline: BaselineAnalyser,
        min_confidence: float = 0.6,
        shadow: bool = False,
        hybrid_routing: bool = True,
        provider_name: str = "llm",
        model_name: str = "",
    ) -> None:
        self._provider = provider
        self._baseline = baseline
        self._min_confidence = min_confidence
        self._shadow = shadow
        # Shadow mode exists to answer "is the model better?", so it must see
        # every eligible utterance — including the ones hybrid routing would
        # skip, which are precisely the ones the baseline already handles well.
        # Skipping them would bias the comparison in the baseline's favour.
        # The extra calls cost latency the caller never experiences, because
        # shadow mode always returns the deterministic result.
        self._hybrid_routing = hybrid_routing and not shadow
        self._labels = {"provider": provider_name, "model": model_name or "unset"}

    @property
    def mode(self) -> str:
        return "shadow" if self._shadow else "llm"

    async def analyse(
        self,
        text: str,
        *,
        vocabulary: Vocabulary,
        today: date,
        state_hint: str | None = None,
    ) -> Analysis:
        started = time.perf_counter()
        # The baseline always runs. It is microseconds, it supplies the lexical
        # signals the model is not asked for (affirmative, negative,
        # correction, question, normalised text), and it is the fallback.
        baseline = await self._baseline.analyse(
            text, vocabulary=vocabulary, today=today, state_hint=state_hint
        )

        try:
            if self._should_skip_model(baseline, state_hint):
                metrics.increment(M.LLM_SKIPPED, labels={"reason": "baseline_confident"})
                return baseline

            llm_result = await self._call_provider(text, state_hint, vocabulary)
            if llm_result is None:
                return baseline

            if self._shadow:
                # Shadow mode: compare and record, then discard. The caller is
                # served the deterministic result, unchanged.
                self._record_shadow_comparison(baseline, llm_result)
                return baseline

            return self._merge(
                baseline,
                llm_result,
                vocabulary=vocabulary,
                today=today,
                state_hint=state_hint,
            )
        finally:
            metrics.observe(M.ANALYSER_LATENCY, (time.perf_counter() - started) * 1000)

    # --- Routing -----------------------------------------------------------
    def _should_skip_model(self, baseline: Analysis, state_hint: str | None) -> bool:
        """Whether the deterministic answer is already good enough.

        Skipping is a latency optimisation, but two of these cases are also
        correctness requirements: an explicit request for a human must never
        depend on a model, and an empty utterance has nothing to classify.
        """
        # Checked before the hybrid-routing switch: this is a correctness rule,
        # not an optimisation, so disabling hybrid routing (as the benchmark
        # does) must not re-enable it.
        if state_hint in _IDENTITY_STATES:
            return True

        if not self._hybrid_routing:
            return False

        # A caller asking for a person is honoured deterministically, always.
        if baseline.intent is Intent.HUMAN_RECEPTION:
            return True
        # Nothing was said. There is nothing for a model to add.
        if baseline.is_empty and not baseline.normalised:
            return True
        # A bare yes/no with no other content — unambiguous and very common.
        if (baseline.affirmative or baseline.negative) and not baseline.entities.has_any():
            return True
        # In selection states a fragment carries meaning the baseline may miss,
        # so consult the model even if it matched something.
        if state_hint in _ALWAYS_CONSULT_STATES:
            return False
        return baseline.confidence >= HYBRID_SKIP_CONFIDENCE

    async def _call_provider(
        self, text: str, state_hint: str | None, vocabulary: Vocabulary
    ) -> LLMAnalysis | None:
        """Call the model. Returns None on any failure — never raises."""
        metrics.increment(M.LLM_REQUESTS, labels=self._labels)
        started = time.perf_counter()
        try:
            result = await self._provider.analyse_message(
                text,
                state=state_hint or "IDENTIFY_INTENT",
                known_practitioners=tuple(p.display_name for p in vocabulary.practitioners),
                known_appointment_types=tuple(t.name for t in vocabulary.appointment_types),
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            reason = failure_reason(exc)
            elapsed = (time.perf_counter() - started) * 1000
            metrics.increment(M.LLM_FAILURE, labels={"reason": reason})
            if reason == "timeout":
                metrics.increment(M.LLM_TIMEOUT)
            elif reason == "invalid_response":
                metrics.increment(M.LLM_INVALID_RESPONSE)
            elif reason == "circuit_open":
                metrics.increment(M.LLM_CIRCUIT_OPEN)
            elif reason == "saturated":
                metrics.increment(M.LLM_SATURATED)
            metrics.increment(M.LLM_FALLBACK, labels={"reason": reason})
            # Reason code and timing only — never the utterance, the prompt or
            # the model's response.
            logger.info(
                "llm.request_failed",
                provider=self._labels["provider"],
                state=state_hint,
                reason=reason,
                duration_ms=round(elapsed),
            )
            return None

        elapsed = (time.perf_counter() - started) * 1000
        metrics.observe(M.LLM_LATENCY, elapsed, labels=self._labels)
        metrics.increment(M.LLM_SUCCESS, labels=self._labels)

        if result.confidence < self._min_confidence:
            metrics.increment(M.LLM_LOW_CONFIDENCE)
            metrics.increment(M.LLM_FALLBACK, labels={"reason": "low_confidence"})
            logger.info(
                "llm.low_confidence",
                provider=self._labels["provider"],
                state=state_hint,
                duration_ms=round(elapsed),
            )
            return None

        logger.debug(
            "llm.request",
            provider=self._labels["provider"],
            state=state_hint,
            result="success",
            duration_ms=round(elapsed),
        )
        return result

    # --- Merging -----------------------------------------------------------
    def _merge(
        self,
        baseline: Analysis,
        llm: LLMAnalysis,
        *,
        vocabulary: Vocabulary,
        today: date,
        state_hint: str | None = None,
    ) -> Analysis:
        """Merge validated model output into the deterministic result."""
        # Entities are resolved *into* the baseline's, filling gaps only, so
        # neither an unresolvable hint nor a confidently wrong one can displace
        # a deterministic parse.
        entities = resolve_hints(
            llm.entities,
            vocabulary=vocabulary,
            today=today,
            into=replace(baseline.entities),
            # Grounding check: a hint that appears nowhere in what the caller
            # said is discarded rather than overwriting a deterministic value.
            utterance_normalised=baseline.normalised,
        )

        intent = llm.intent
        confidence = llm.confidence

        # In an answer state the agent has asked a question and this utterance
        # is the answer, so a task intent from the model is not a task switch —
        # it is a misreading. Suppressing it here keeps the Milestone 1
        # semantics, where the deterministic analyser returned UNKNOWN for an
        # answer and the engine kept the active flow.
        #
        # Found by live testing: mid-reschedule, the model classified the date
        # of birth "14 March 1958" as BOOK_APPOINTMENT, and the engine dutifully
        # switched the caller out of their reschedule and into a new booking.
        # A genuine mid-flow switch ("actually, cancel it instead") is still
        # caught by the deterministic analyser, which is authoritative here.
        if (
            state_hint is not None
            and state_hint not in _INTENT_OPEN_STATES
            and intent not in _ALWAYS_ALLOWED_INTENTS
            and intent is not baseline.intent
        ):
            intent = baseline.intent
            confidence = baseline.confidence

        # Escalation is a one-way ratchet. The model may raise a transfer it
        # spotted, but it can never clear one the deterministic layer found.
        #
        # This guard is enforced here rather than relying on hybrid routing to
        # skip the model: routing is configurable and is deliberately disabled
        # when benchmarking, so leaning on it would make the invariant depend
        # on a performance setting. A caller asking for a person gets a person.
        if baseline.intent is Intent.HUMAN_RECEPTION:
            intent = Intent.HUMAN_RECEPTION
            confidence = max(confidence, baseline.confidence)
        elif llm.requires_human and intent is not Intent.HUMAN_RECEPTION:
            intent = Intent.HUMAN_RECEPTION
            confidence = max(confidence, 0.9)

        # An UNKNOWN from the model must not erase a deterministic match.
        if intent is Intent.UNKNOWN and baseline.intent is not Intent.UNKNOWN:
            intent = baseline.intent
            confidence = baseline.confidence

        return Analysis(
            intent=intent,
            confidence=confidence,
            entities=entities,
            # Lexical signals stay deterministic: these are exact string
            # matches, and a model adds nothing but latency and risk.
            affirmative=baseline.affirmative,
            negative=baseline.negative,
            is_correction=baseline.is_correction,
            is_question=baseline.is_question,
            is_empty=(
                intent is Intent.UNKNOWN
                and not entities.has_any()
                and not baseline.affirmative
                and not baseline.negative
            ),
            normalised=baseline.normalised,
        )

    # --- Shadow ------------------------------------------------------------
    def _record_shadow_comparison(self, baseline: Analysis, llm: LLMAnalysis) -> None:
        """Record agreement only.

        The utterance, the hints and the resolved entities are all discarded.
        What is kept is two enum values and a confidence bucket, which is
        enough to answer "is the model better?" without retaining anything
        about the caller.
        """
        agrees = baseline.intent is llm.intent
        labels = {
            "baseline": baseline.intent.value,
            "llm": llm.intent.value,
            "confidence": _confidence_bucket(llm.confidence),
        }
        if agrees:
            metrics.increment(M.LLM_SHADOW_AGREEMENT, labels={"intent": llm.intent.value})
        else:
            metrics.increment(M.LLM_SHADOW_DISAGREEMENT, labels=labels)


def _confidence_bucket(confidence: float) -> str:
    if confidence >= 0.9:
        return "0.9-1.0"
    if confidence >= 0.8:
        return "0.8-0.9"
    if confidence >= 0.6:
        return "0.6-0.8"
    return "<0.6"
