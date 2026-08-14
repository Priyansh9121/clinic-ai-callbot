"""Where the receptionist's spoken answers come from.

Knowledge entries are read to callers verbatim, so the source is a safety
control. The development set is fictional — "Riverside Medical Clinic" has no
address, no fees and no bulk-billing policy — and telephony was using it as
its production knowledge source with nothing to say so.
"""

from __future__ import annotations

import json

import pytest

from app.clinic.factory import build_clinic_knowledge, load_knowledge_file
from app.core.config import Settings
from app.core.errors import ProviderUnavailableError

APPROVED = [
    {
        "topic": "OPENING_HOURS",
        "answer": "We are open 8am to 6pm, Monday to Friday.",
        "keywords": ["opening hours", "what time"],
    },
    {
        "topic": "TEST_RESULTS",
        "answer": "I will put you through to reception for results.",
        "keywords": ["results"],
        "requires_human": True,
    },
]


def _settings(**overrides) -> Settings:
    """Settings that satisfy the production hardening validator.

    Those checks are about secrets and session storage, not knowledge, but
    they run first — so a production case has to clear them to reach the
    behaviour under test.
    """
    if overrides.get("app_env") == "production":
        overrides = {
            "app_secret_key": "a-real-production-secret-value-not-the-default",
            "redis_url": "redis://127.0.0.1:6379/0",
            "app_debug": False,
            "staff_api_keys": "admin:a-real-staff-key-value",
            "cors_allowed_origins": "https://clinic.example",
            **overrides,
        }
    return Settings(_env_file=None, **overrides)


def test_production_refuses_to_speak_placeholder_content() -> None:
    """The failure a clinic cannot afford is this one happening silently."""
    settings = _settings(app_env="production", clinic_knowledge_file="")

    with pytest.raises(ProviderUnavailableError, match="CLINIC_KNOWLEDGE_FILE"):
        build_clinic_knowledge(settings)


def test_development_falls_back_to_the_placeholder_set() -> None:
    """Everything must still run without a clinic's real content."""
    knowledge = build_clinic_knowledge(_settings(app_env="development"))

    assert knowledge.entries, "development needs answerable topics to exercise the flows"


def test_an_approved_file_replaces_the_placeholder_set(tmp_path) -> None:
    path = tmp_path / "knowledge.json"
    path.write_text(json.dumps(APPROVED))

    knowledge = build_clinic_knowledge(
        _settings(app_env="production", clinic_knowledge_file=str(path))
    )

    assert len(knowledge.entries) == 2
    assert knowledge.entries[0].answer.startswith("We are open")
    # requires_human survives the round trip: it is what stops the bot
    # answering a question about test results itself.
    assert knowledge.entries[1].requires_human is True


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ("not json at all", "valid JSON"),
        ("{}", "non-empty JSON list"),
        ("[]", "non-empty JSON list"),
        ('[{"topic": "NOT_A_TOPIC", "answer": "hi"}]', "invalid"),
        ('[{"topic": "OPENING_HOURS"}]', "invalid"),
        ('[{"topic": "OPENING_HOURS", "answer": "   "}]', "empty answer"),
    ],
)
def test_a_broken_knowledge_file_fails_at_startup(tmp_path, content, expected) -> None:
    """Not at call time, where it becomes a caller hearing nothing useful."""
    path = tmp_path / "knowledge.json"
    path.write_text(content)

    with pytest.raises(ProviderUnavailableError, match=expected):
        load_knowledge_file(path)


def test_a_missing_knowledge_file_names_the_setting(tmp_path) -> None:
    with pytest.raises(ProviderUnavailableError, match="CLINIC_KNOWLEDGE_FILE"):
        load_knowledge_file(tmp_path / "absent.json")
