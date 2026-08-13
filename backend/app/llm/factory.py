"""Construction of the provider and the analyser from configuration.

This is the only place that maps a configured provider name to a concrete
runtime. Routes, the engine and the flows ask for a ``MessageAnalyser`` and
never learn which one they got.

Startup is deliberately resilient: nothing here reaches the network. A
misconfiguration degrades to the deterministic analyser and is logged; an
unreachable model is discovered on the first turn and handled by the fallback
path. Neither prevents the backend from answering the phone.
"""

from __future__ import annotations

from app.conversation.nlu.baseline import BaselineAnalyser, MessageAnalyser
from app.conversation.nlu.llm_analyser import LLMBackedAnalyser
from app.core.config import Settings
from app.core.logging import get_logger
from app.llm.provider import LLMProvider

logger = get_logger(__name__)


def build_llm_provider(settings: Settings) -> LLMProvider | None:
    """Instantiate the configured provider, or None when disabled."""
    if not settings.llm_configured:
        return None

    if settings.llm_provider == "ollama":
        from app.llm.ollama import OllamaLLMProvider

        return OllamaLLMProvider(
            base_url=settings.llm_base_url,
            model=settings.llm_model,
            timeout_seconds=settings.llm_timeout_seconds,
            max_output_tokens=settings.llm_max_output_tokens,
            temperature=settings.llm_temperature,
            max_concurrency=settings.llm_max_concurrency,
            failure_threshold=settings.llm_failure_threshold,
            recovery_seconds=settings.llm_recovery_seconds,
            keep_alive=settings.llm_keep_alive,
        )

    # Declared in configuration but not implemented yet (llama.cpp). Degrade
    # rather than crash: the deterministic analyser is still available.
    logger.warning(
        "llm.provider_not_implemented",
        provider=settings.llm_provider,
        detail="falling back to the deterministic analyser",
    )
    return None


def build_message_analyser(
    settings: Settings,
    *,
    provider: LLMProvider | None = None,
    baseline: BaselineAnalyser | None = None,
) -> MessageAnalyser:
    """Build the analyser for the configured mode.

    ``baseline`` and ``llm``/``shadow`` all return something satisfying
    ``MessageAnalyser``; the caller cannot tell them apart.
    """
    baseline = baseline or BaselineAnalyser()
    mode = settings.effective_llm_mode

    if mode != settings.llm_mode:
        logger.warning(
            "llm.mode_downgraded",
            requested=settings.llm_mode,
            effective=mode,
            detail="LLM_PROVIDER/LLM_MODEL not configured",
        )

    if mode == "baseline":
        logger.info("llm.analyser", mode="baseline")
        return baseline

    if provider is None:
        provider = build_llm_provider(settings)
    if provider is None:
        logger.warning("llm.analyser", mode="baseline", detail="no provider available")
        return baseline

    logger.info(
        "llm.analyser",
        mode=mode,
        provider=settings.llm_provider,
        model=settings.llm_model,
        min_confidence=settings.llm_min_confidence,
        hybrid_routing=settings.llm_hybrid_routing,
    )
    return LLMBackedAnalyser(
        provider=provider,
        baseline=baseline,
        min_confidence=settings.llm_min_confidence,
        shadow=(mode == "shadow"),
        hybrid_routing=settings.llm_hybrid_routing,
        provider_name=settings.llm_provider,
        model_name=settings.llm_model,
    )
