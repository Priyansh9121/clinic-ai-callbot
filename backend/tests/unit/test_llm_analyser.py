"""The LLM-backed analyser: modes, fallback, merging and hint resolution.

Offline throughout — a scripted ``FakeLLMProvider`` stands in for the model.
"""

from __future__ import annotations

import uuid
from datetime import date

import pytest

from app.conversation.nlu.baseline import (
    AppointmentTypeTerm,
    BaselineAnalyser,
    PractitionerTerm,
    Vocabulary,
)
from app.conversation.nlu.llm_analyser import LLMBackedAnalyser
from app.conversation.states import Intent
from app.core.metrics import MetricsRegistry
from app.core.timeutils import DayPeriod
from tests.fixtures.fake_llm import PROVIDER_FAILURES, FakeLLMProvider, analysis

TODAY = date(2026, 8, 12)  # Wednesday

PATEL, WILSON = uuid.uuid4(), uuid.uuid4()
STANDARD, TELEHEALTH = uuid.uuid4(), uuid.uuid4()

VOCABULARY = Vocabulary(
    practitioners=(
        PractitionerTerm(
            PATEL,
            "Dr Sarah Patel",
            ("dr sarah patel", "doctor patel", "sarah patel", "dr patel", "patel"),
        ),
        PractitionerTerm(
            WILSON, "Dr James Wilson", ("dr james wilson", "doctor wilson", "dr wilson", "wilson")
        ),
    ),
    appointment_types=(
        AppointmentTypeTerm(
            STANDARD,
            "standard_gp",
            "Standard GP",
            ("standard appointment", "standard gp", "standard"),
        ),
        AppointmentTypeTerm(
            TELEHEALTH,
            "telehealth",
            "Telehealth",
            ("telehealth", "phone appointment", "over the phone"),
        ),
    ),
)


def build(provider, **kwargs) -> LLMBackedAnalyser:
    kwargs.setdefault("hybrid_routing", False)  # exercise the model path
    return LLMBackedAnalyser(provider=provider, baseline=BaselineAnalyser(), **kwargs)


async def run(analyser, text: str, state: str = "IDENTIFY_INTENT"):
    return await analyser.analyse(text, vocabulary=VOCABULARY, today=TODAY, state_hint=state)


# --- Fallback matrix --------------------------------------------------------
@pytest.mark.parametrize(
    ("label", "failure"), PROVIDER_FAILURES, ids=[label for label, _ in PROVIDER_FAILURES]
)
async def test_every_provider_failure_falls_back(label: str, failure: Exception) -> None:
    """No crash, no dead end — the deterministic answer is served instead."""
    analyser = build(FakeLLMProvider([failure]))
    result = await run(analyser, "I need to cancel my appointment")

    assert result.intent is Intent.CANCEL_APPOINTMENT  # from the baseline
    assert result.normalised


async def test_low_confidence_falls_back_to_the_baseline() -> None:
    analyser = build(
        FakeLLMProvider([analysis(Intent.CLINIC_INFORMATION, confidence=0.2)]),
        min_confidence=0.6,
    )
    result = await run(analyser, "I need to cancel my appointment")
    assert result.intent is Intent.CANCEL_APPOINTMENT


@pytest.mark.parametrize(
    ("confidence", "expect_llm"),
    [(0.79, False), (0.80, True), (0.81, True)],
)
async def test_confidence_threshold_boundary(confidence: float, expect_llm: bool) -> None:
    """The configured threshold is applied at exactly the configured value."""
    analyser = build(
        FakeLLMProvider([analysis(Intent.CLINIC_INFORMATION, confidence=confidence)]),
        min_confidence=0.80,
    )
    result = await run(analyser, "I need to cancel my appointment")
    expected = Intent.CLINIC_INFORMATION if expect_llm else Intent.CANCEL_APPOINTMENT
    assert result.intent is expected


async def test_provider_is_never_allowed_to_raise_through() -> None:
    class Exploding:
        async def analyse_message(self, *args, **kwargs):
            raise MemoryError("catastrophic")

        async def health(self) -> bool:
            return False

    result = await run(build(Exploding()), "book me an appointment")
    assert result.intent is Intent.BOOK_APPOINTMENT


# --- Modes ------------------------------------------------------------------
async def test_shadow_mode_does_not_change_the_caller_result() -> None:
    provider = FakeLLMProvider([analysis(Intent.CLINIC_INFORMATION, confidence=0.99)])
    analyser = build(provider, shadow=True)

    result = await run(analyser, "I need to cancel my appointment")

    # The model said CLINIC_INFORMATION; the deterministic answer still wins.
    assert result.intent is Intent.CANCEL_APPOINTMENT
    assert provider.call_count == 1  # but it was consulted, for comparison
    assert analyser.mode == "shadow"


async def test_shadow_mode_records_agreement_without_utterances() -> None:
    from app.core import metrics as metrics_module

    registry = MetricsRegistry()
    original = metrics_module.metrics
    metrics_module.metrics = registry
    try:
        import app.conversation.nlu.llm_analyser as analyser_module

        analyser_module.metrics = registry
        provider = FakeLLMProvider(
            [
                analysis(Intent.CANCEL_APPOINTMENT, confidence=0.9),  # agrees
                analysis(Intent.CLINIC_INFORMATION, confidence=0.9),  # disagrees
            ]
        )
        analyser = build(provider, shadow=True)
        await run(analyser, "I need to cancel my appointment")
        await run(analyser, "I need to cancel my appointment")

        snapshot = str(registry.snapshot())
        assert "shadow_agreement" in snapshot
        assert "shadow_disagreement" in snapshot
        # Only enum values and buckets — never the caller's words.
        assert "cancel my appointment" not in snapshot
    finally:
        metrics_module.metrics = original
        import app.conversation.nlu.llm_analyser as analyser_module

        analyser_module.metrics = original


async def test_llm_mode_uses_the_model_result() -> None:
    analyser = build(FakeLLMProvider([analysis(Intent.RESCHEDULE_APPOINTMENT, confidence=0.95)]))
    result = await run(analyser, "something's come up")
    assert result.intent is Intent.RESCHEDULE_APPOINTMENT
    assert analyser.mode == "llm"


# --- Hybrid routing ---------------------------------------------------------
async def test_explicit_human_request_never_consults_the_model() -> None:
    """A caller asking for a person must not depend on a model being up."""
    provider = FakeLLMProvider([analysis(Intent.BOOK_APPOINTMENT, confidence=0.99)])
    analyser = build(provider, hybrid_routing=True)

    result = await run(analyser, "let me speak to someone")

    assert result.intent is Intent.HUMAN_RECEPTION
    assert provider.call_count == 0


@pytest.mark.parametrize("utterance", ["yes", "no", "yep", "nope", "that's right"])
async def test_bare_yes_no_skips_the_model(utterance: str) -> None:
    """Latency: a one-word confirmation does not need a model."""
    provider = FakeLLMProvider([analysis(Intent.BOOK_APPOINTMENT, confidence=0.99)])
    analyser = build(provider, hybrid_routing=True)
    await analyser.analyse(
        utterance, vocabulary=VOCABULARY, today=TODAY, state_hint="CONFIRM_BOOKING"
    )
    assert provider.call_count == 0


async def test_ambiguous_utterances_do_consult_the_model() -> None:
    provider = FakeLLMProvider([analysis(Intent.BOOK_APPOINTMENT, confidence=0.9)])
    analyser = build(provider, hybrid_routing=True)
    await run(analyser, "was hoping to get in sometime friday")
    assert provider.call_count == 1


async def test_selection_states_always_consult_the_model() -> None:
    provider = FakeLLMProvider(
        [analysis(Intent.BOOK_APPOINTMENT, confidence=0.9, selection="the second one")]
    )
    analyser = build(provider, hybrid_routing=True)
    await run(analyser, "the second one", state="SELECT_SLOT")
    assert provider.call_count == 1


# --- Merging ----------------------------------------------------------------
async def test_resolved_hints_are_overlaid_onto_the_baseline() -> None:
    analyser = build(
        FakeLLMProvider(
            [
                analysis(
                    Intent.BOOK_APPOINTMENT,
                    confidence=0.95,
                    practitioner="Dr Patel",
                    preferred_date_text="next Friday",
                    preferred_period="afternoon",
                )
            ]
        )
    )
    result = await run(analyser, "any chance patel's around next friday arvo")

    assert result.entities.practitioner_id == PATEL
    assert result.entities.preferred_date == date(2026, 8, 21)  # resolved in code
    assert result.entities.preferred_period is DayPeriod.AFTERNOON


async def test_an_unresolvable_hint_leaves_the_baseline_value_intact() -> None:
    """The merged result is never worse than the deterministic one."""
    analyser = build(
        FakeLLMProvider(
            [analysis(Intent.BOOK_APPOINTMENT, confidence=0.95, practitioner="Dr Harry Potter")]
        )
    )
    # The baseline already found Wilson from the text.
    result = await run(analyser, "I'd like to see Dr Wilson tomorrow")
    assert result.entities.practitioner_id == WILSON


async def test_model_unknown_does_not_erase_a_deterministic_intent() -> None:
    analyser = build(FakeLLMProvider([analysis(Intent.UNKNOWN, confidence=0.9)]))
    result = await run(analyser, "I need to cancel my appointment")
    assert result.intent is Intent.CANCEL_APPOINTMENT


async def test_lexical_signals_stay_deterministic() -> None:
    """affirmative/negative/correction come from exact matching, not the model."""
    analyser = build(FakeLLMProvider([analysis(Intent.BOOK_APPOINTMENT, confidence=0.95)]))
    result = await run(analyser, "actually make it Thursday", state="SELECT_SLOT")
    assert result.is_correction is True


# --- requires_human ---------------------------------------------------------
async def test_requires_human_true_escalates() -> None:
    analyser = build(
        FakeLLMProvider([analysis(Intent.BOOK_APPOINTMENT, confidence=0.9, requires_human=True)])
    )
    result = await run(analyser, "I'd rather not deal with a machine about this")
    assert result.intent is Intent.HUMAN_RECEPTION


async def test_requires_human_false_cannot_cancel_a_deterministic_escalation() -> None:
    """The invariant: the model may escalate, never de-escalate."""
    provider = FakeLLMProvider(
        [analysis(Intent.BOOK_APPOINTMENT, confidence=0.99, requires_human=False)]
    )
    analyser = build(provider, hybrid_routing=True)

    result = await run(analyser, "can I talk to a person")

    assert result.intent is Intent.HUMAN_RECEPTION
    assert provider.call_count == 0  # not even consulted


async def test_requires_human_false_does_not_override_baseline_when_consulted() -> None:
    """Even with hybrid routing off, a baseline human request survives."""
    provider = FakeLLMProvider(
        [analysis(Intent.BOOK_APPOINTMENT, confidence=0.99, requires_human=False)]
    )
    analyser = build(provider, hybrid_routing=False)
    result = await run(analyser, "put me through to reception")
    # The model was consulted and said BOOK; the deterministic detection of an
    # explicit human request is restored by the merge guard.
    assert result.intent is Intent.HUMAN_RECEPTION


# --- Privacy ----------------------------------------------------------------
async def test_only_permitted_context_reaches_the_provider() -> None:
    provider = FakeLLMProvider([analysis(Intent.BOOK_APPOINTMENT, confidence=0.9)])
    analyser = build(provider)
    await run(analyser, "friday afternoon would suit", state="PREFERRED_DATE")

    call = provider.calls[0]
    assert set(call) == {"message", "state", "known_practitioners", "known_appointment_types"}
    # The vocabulary contains clinic staff, never patients.
    assert all("Hughes" not in p for p in call["known_practitioners"])
    assert call["state"] == "PREFERRED_DATE"


# --- Identity turns never reach the model -----------------------------------
@pytest.mark.parametrize("state", ["VERIFY_PATIENT", "NEW_PATIENT_DETAILS"])
async def test_identity_turns_never_reach_the_model(state: str) -> None:
    """Regression, found in live testing.

    Given "14 March 1958" the model returned
    ``preferred_date_text="14 March 1958"``, which resolved forward to 14 March
    2027 and became the *appointment* date — a date with no availability, so
    the booking silently failed several turns later. A date of birth is not an
    appointment preference, and the model has nothing to offer on an identity
    turn anyway.
    """
    provider = FakeLLMProvider(
        [analysis(Intent.BOOK_APPOINTMENT, confidence=0.99, preferred_date_text="14 March 1958")]
    )
    analyser = build(provider, hybrid_routing=False)  # correctness, not an optimisation

    result = await analyser.analyse(
        "14 March 1958", vocabulary=VOCABULARY, today=TODAY, state_hint=state
    )

    assert provider.call_count == 0
    assert result.entities.preferred_date is None


async def test_a_date_of_birth_is_never_read_as_an_appointment_date() -> None:
    provider = FakeLLMProvider(
        [analysis(Intent.BOOK_APPOINTMENT, confidence=0.99, preferred_date_text="14 March 1958")]
    )
    analyser = build(provider, hybrid_routing=False)
    result = await analyser.analyse(
        "Hughes, 14 March 1958",
        vocabulary=VOCABULARY,
        today=TODAY,
        state_hint="VERIFY_PATIENT",
    )
    assert result.entities.preferred_date is None
    # The deterministic identity extraction still ran.
    assert result.entities.date_of_birth is not None
