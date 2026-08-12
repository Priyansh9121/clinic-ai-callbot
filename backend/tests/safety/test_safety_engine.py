"""Safety engine tests.

The most important property under test is *priority*: a safety trigger must win
over any appointment or information intent present in the same sentence.
"""

from __future__ import annotations

import pytest

from app.safety.engine import (
    SafetyEngine,
    emergency_response,
    escalation_response,
    normalise,
)
from app.safety.models import SafetyAction, SafetyCategory


@pytest.fixture
def engine() -> SafetyEngine:
    return SafetyEngine()


EMERGENCY_UTTERANCES = [
    "I've got really bad chest pain",
    "my husband has chest pains and is sweating",
    "I can't breathe properly",
    "she cannot breathe",
    "he's not breathing",
    "my father is unconscious",
    "my son collapsed and won't wake up",
    "there's severe bleeding from his leg",
    "the cut won't stop bleeding",
    "I think my mum is having a stroke",
    "her face is drooping and her speech is slurred",
    "he took too many pills",
    "I think it's an overdose",
    "she's having an allergic reaction and her throat is closing",
    "my daughter is having a seizure",
    "I was in a car accident",
]


@pytest.mark.parametrize("utterance", EMERGENCY_UTTERANCES)
def test_emergencies_are_detected(engine: SafetyEngine, utterance: str) -> None:
    verdict = engine.evaluate(utterance)
    assert verdict.is_emergency, f"missed emergency: {utterance!r}"
    assert verdict.rule_id is not None


SELF_HARM_UTTERANCES = [
    "I want to kill myself",
    "I'm thinking about ending my life",
    "I've been hurting myself",
    "I don't want to live anymore",
]


@pytest.mark.parametrize("utterance", SELF_HARM_UTTERANCES)
def test_self_harm_is_an_emergency(engine: SafetyEngine, utterance: str) -> None:
    verdict = engine.evaluate(utterance)
    assert verdict.is_emergency
    assert verdict.category is SafetyCategory.SELF_HARM


def test_self_harm_response_offers_a_crisis_line() -> None:
    reply = emergency_response(SafetyCategory.SELF_HARM, "000")
    assert "13 11 14" in reply  # Lifeline
    assert "triple zero" in reply.lower()


def test_emergency_response_directs_to_triple_zero() -> None:
    reply = emergency_response(SafetyCategory.MEDICAL_EMERGENCY, "000")
    assert "triple zero" in reply.lower()
    assert "000" in reply


def test_emergency_response_never_gives_medical_advice() -> None:
    """The wording is fixed so it cannot drift into treatment instructions."""
    for category in (SafetyCategory.MEDICAL_EMERGENCY, SafetyCategory.SELF_HARM):
        reply = emergency_response(category, "000").lower()
        for forbidden in (
            "take ",
            "dose",
            "mg",
            "aspirin",
            "you should probably",
            "it's probably",
            "wait and see",
            "you'll be fine",
        ):
            assert forbidden not in reply


ESCALATION_UTTERANCES = [
    ("what should I do about this rash", SafetyCategory.CLINICAL_ADVICE),
    ("is this serious", SafetyCategory.CLINICAL_ADVICE),
    ("what's wrong with me", SafetyCategory.CLINICAL_ADVICE),
    ("how many should I take", SafetyCategory.MEDICATION),
    ("what's the dosage for my blood pressure tablets", SafetyCategory.MEDICATION),
    ("can I take ibuprofen with my other tablets", SafetyCategory.MEDICATION),
    ("are my blood test results back", SafetyCategory.TEST_RESULTS),
    ("I'm in agony", SafetyCategory.DISTRESS),
]


@pytest.mark.parametrize(("utterance", "category"), ESCALATION_UTTERANCES)
def test_escalation_topics_require_a_human(
    engine: SafetyEngine, utterance: str, category: SafetyCategory
) -> None:
    verdict = engine.evaluate(utterance)
    assert verdict.requires_human
    assert verdict.category is category


BENIGN_UTTERANCES = [
    "I'd like to book an appointment",
    "can I see Dr Patel on Friday",
    "what time do you open",
    "I need to cancel my appointment tomorrow",
    "do you bulk bill",
    "my name is Margaret Hughes",
    "anything on Thursday afternoon",
    "actually make it Thursday",
    "the first one please",
    "yes that's right",
]


@pytest.mark.parametrize("utterance", BENIGN_UTTERANCES)
def test_ordinary_requests_are_not_flagged(engine: SafetyEngine, utterance: str) -> None:
    verdict = engine.evaluate(utterance)
    assert verdict.is_clear, f"false positive on {utterance!r}: {verdict.rule_id}"


def test_administrative_mentions_are_suppressed(engine: SafetyEngine) -> None:
    """A follow-up *about* a past event is not an active emergency."""
    assert engine.evaluate("I need a follow-up about my chest pain last month").is_clear
    assert engine.evaluate("I have a stroke clinic review booked").is_clear


def test_emergency_beats_a_booking_intent_in_the_same_sentence(engine: SafetyEngine) -> None:
    verdict = engine.evaluate("I want to book an appointment, I'm having chest pain")
    assert verdict.is_emergency


def test_emergency_beats_an_escalation_topic(engine: SafetyEngine) -> None:
    """Ordering guarantees the more severe rule wins."""
    verdict = engine.evaluate("what should I do, I can't breathe")
    assert verdict.action is SafetyAction.EMERGENCY


def test_empty_input_is_clear(engine: SafetyEngine) -> None:
    assert engine.evaluate("").is_clear
    assert engine.evaluate("   ").is_clear


def test_normalisation_defeats_punctuation_and_case(engine: SafetyEngine) -> None:
    """A caller cannot dodge a rule with punctuation or capitals."""
    for variant in ("CHEST PAIN!!!", "chest-pain", "chest   pain", "Chest, pain."):
        assert engine.evaluate(variant).is_emergency, variant


def test_normalise_collapses_contractions() -> None:
    assert normalise("I can't breathe") == "i cant breathe"
    assert normalise("I can not breathe") == "i cannot breathe"


class _AlwaysEmergencyClassifier:
    async def classify(self, text: str):
        from app.safety.models import SafetyVerdict

        return SafetyVerdict(
            action=SafetyAction.EMERGENCY,
            category=SafetyCategory.MEDICAL_EMERGENCY,
            rule_id="MODEL",
        )


class _BrokenClassifier:
    async def classify(self, text: str):
        raise RuntimeError("model is down")


class _DowngradingClassifier:
    async def classify(self, text: str):
        from app.safety.models import SafetyVerdict

        return SafetyVerdict()  # says "all clear"


async def test_classifier_may_escalate() -> None:
    engine = SafetyEngine(classifier=_AlwaysEmergencyClassifier())
    verdict = await engine.evaluate_async("I feel a bit odd today")
    assert verdict.is_emergency


async def test_classifier_may_not_downgrade_a_rule_hit() -> None:
    """A model must never be able to clear an emergency the rules found."""
    engine = SafetyEngine(classifier=_DowngradingClassifier())
    verdict = await engine.evaluate_async("I have chest pain")
    assert verdict.is_emergency


async def test_classifier_failure_falls_back_to_rules() -> None:
    engine = SafetyEngine(classifier=_BrokenClassifier())
    assert (await engine.evaluate_async("I have chest pain")).is_emergency
    assert (await engine.evaluate_async("book an appointment")).is_clear


def test_escalation_responses_offer_reception() -> None:
    for category in (
        SafetyCategory.CLINICAL_ADVICE,
        SafetyCategory.MEDICATION,
        SafetyCategory.TEST_RESULTS,
        SafetyCategory.DISTRESS,
    ):
        assert "reception" in escalation_response(category).lower()
