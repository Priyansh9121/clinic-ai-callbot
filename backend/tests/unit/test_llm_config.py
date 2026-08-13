"""LLM configuration and factory wiring."""

from __future__ import annotations

import pytest

from app.conversation.nlu.baseline import BaselineAnalyser
from app.conversation.nlu.llm_analyser import LLMBackedAnalyser
from app.core.config import Settings
from app.llm.factory import build_llm_provider, build_message_analyser
from app.llm.ollama import OllamaLLMProvider

VALID_DSN = "postgresql+asyncpg://u:p@localhost:5432/db"


def settings(**overrides) -> Settings:
    """Hermetic settings.

    ``_env_file=None`` stops the developer's local .env leaking in — otherwise
    these assertions describe whoever ran them rather than the shipped
    defaults.
    """
    base = {
        "app_env": "test",
        "database_url": VALID_DSN,
        "redis_url": "",
        "_env_file": None,
    }
    return Settings(**{**base, **overrides})


# --- Configuration ----------------------------------------------------------
def test_llm_is_disabled_by_default() -> None:
    """Milestone 1 behaviour is the default: no model unless asked for."""
    config = settings()
    assert config.llm_provider == "none"
    assert config.llm_mode == "baseline"
    assert config.llm_configured is False
    assert config.effective_llm_mode == "baseline"


def test_a_provider_without_a_model_is_not_configured() -> None:
    assert settings(llm_provider="ollama", llm_model="").llm_configured is False
    assert settings(llm_provider="ollama", llm_model="   ").llm_configured is False


def test_a_fully_specified_provider_is_configured() -> None:
    config = settings(llm_provider="ollama", llm_model="qwen2.5:1.5b-instruct", llm_mode="llm")
    assert config.llm_configured is True
    assert config.effective_llm_mode == "llm"


@pytest.mark.parametrize("mode", ["llm", "shadow"])
def test_a_mode_without_a_provider_degrades_to_baseline(mode: str) -> None:
    """A misconfiguration must not stop the clinic answering the phone."""
    config = settings(llm_provider="none", llm_mode=mode)
    assert config.effective_llm_mode == "baseline"


@pytest.mark.parametrize("mode", ["baseline", "llm", "shadow"])
def test_all_modes_are_accepted(mode: str) -> None:
    assert settings(llm_mode=mode).llm_mode == mode


def test_an_unknown_mode_is_rejected() -> None:
    with pytest.raises(ValueError, match="llm_mode"):
        settings(llm_mode="creative")


def test_an_unknown_provider_is_rejected() -> None:
    with pytest.raises(ValueError, match="llm_provider"):
        settings(llm_provider="openai")


def test_llm_defaults_are_sane_for_a_phone_call() -> None:
    config = settings()
    # A caller is waiting: a multi-second budget is already generous.
    assert config.llm_timeout_seconds <= 5.0
    assert 0.0 <= config.llm_min_confidence <= 1.0
    assert config.llm_max_concurrency >= 1
    assert config.llm_max_output_tokens <= 500
    assert config.llm_temperature == 0.0  # deterministic classification


# --- Factory ----------------------------------------------------------------
def test_no_provider_is_built_when_disabled() -> None:
    assert build_llm_provider(settings()) is None


def test_an_ollama_provider_is_built_when_configured() -> None:
    provider = build_llm_provider(
        settings(llm_provider="ollama", llm_model="qwen2.5:1.5b-instruct")
    )
    assert isinstance(provider, OllamaLLMProvider)
    assert provider.model == "qwen2.5:1.5b-instruct"


def test_an_unimplemented_provider_degrades_rather_than_crashing() -> None:
    """llama.cpp is declared in configuration but not implemented yet."""
    assert build_llm_provider(settings(llm_provider="llamacpp", llm_model="x")) is None


def test_baseline_mode_returns_the_deterministic_analyser() -> None:
    analyser = build_message_analyser(settings(llm_mode="baseline"))
    assert isinstance(analyser, BaselineAnalyser)


def test_llm_mode_returns_the_llm_analyser() -> None:
    analyser = build_message_analyser(
        settings(llm_provider="ollama", llm_model="m", llm_mode="llm")
    )
    assert isinstance(analyser, LLMBackedAnalyser)
    assert analyser.mode == "llm"


def test_shadow_mode_returns_a_shadow_analyser() -> None:
    analyser = build_message_analyser(
        settings(llm_provider="ollama", llm_model="m", llm_mode="shadow")
    )
    assert isinstance(analyser, LLMBackedAnalyser)
    assert analyser.mode == "shadow"


def test_a_misconfigured_mode_falls_back_to_the_baseline_analyser() -> None:
    analyser = build_message_analyser(settings(llm_provider="none", llm_mode="llm"))
    assert isinstance(analyser, BaselineAnalyser)


def test_the_factory_touches_no_network() -> None:
    """Startup must not depend on the model being reachable.

    Pointing at a port with nothing on it still builds successfully; the
    failure surfaces on the first turn and is absorbed by the fallback.
    """
    analyser = build_message_analyser(
        settings(
            llm_provider="ollama",
            llm_model="m",
            llm_mode="llm",
            llm_base_url="http://127.0.0.1:9",
        )
    )
    assert isinstance(analyser, LLMBackedAnalyser)


def test_the_analyser_satisfies_the_message_analyser_protocol() -> None:
    from app.conversation.nlu.baseline import MessageAnalyser

    analyser = build_message_analyser(
        settings(llm_provider="ollama", llm_model="m", llm_mode="llm")
    )
    assert isinstance(analyser, MessageAnalyser)
