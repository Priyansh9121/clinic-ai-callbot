"""Health and metrics endpoints.

``GET /health`` is what the telephony layer calls before routing a call to the
AI. If it does not return ``ok``, Asterisk sends the caller straight to
reception (see docs/telephony/fallback.md) — so this endpoint must be cheap,
must never block, and must reflect the components a call actually needs.
"""

from __future__ import annotations

import time
from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy import text

from app.api.deps import AppSettings, DbSession, get_session_store
from app.conversation.store import SessionStore
from app.core.metrics import metrics
from app.schemas.common import HealthComponent, HealthResponse

router = APIRouter(tags=["health"])

VERSION = "0.1.0"


async def _llm_component(request: Request, settings: AppSettings) -> HealthComponent:
    """Real LLM status.

    Deliberately does **not** gate the overall health status. The deterministic
    analyser handles every turn on its own, so:

        LLM down  =  degraded intelligence, not a service outage

    Treating it as an outage would send callers to reception over a failure the
    system is designed to absorb — and, once telephony lands, the dialplan
    routes on this endpoint. Detail is limited to provider and model names.
    """
    if not settings.llm_configured:
        return HealthComponent(
            name="llm",
            status="not_configured",
            detail=f"provider={settings.llm_provider} mode={settings.effective_llm_mode}",
        )

    detail = (
        f"provider={settings.llm_provider} model={settings.llm_model} "
        f"mode={settings.effective_llm_mode}"
    )
    provider = getattr(request.app.state, "llm_provider", None)
    if provider is None:
        return HealthComponent(name="llm", status="down", detail=detail)

    started = time.perf_counter()
    try:
        reachable = await provider.health()
    except Exception as exc:
        return HealthComponent(
            name="llm", status="down", detail=f"{detail} error={type(exc).__name__}"
        )

    if getattr(provider, "circuit_open", False):
        return HealthComponent(
            name="llm",
            status="degraded",
            detail=f"{detail} circuit=open",
            latency_ms=round((time.perf_counter() - started) * 1000, 2),
        )
    return HealthComponent(
        name="llm",
        status="ok" if reachable else "down",
        detail=detail,
        latency_ms=round((time.perf_counter() - started) * 1000, 2),
    )


@router.get("/health", response_model=HealthResponse)
async def health(
    request: Request,
    response: Response,
    db: DbSession,
    settings: AppSettings,
    store: Annotated[SessionStore, Depends(get_session_store)],
) -> HealthResponse:
    components: list[HealthComponent] = []

    started = time.perf_counter()
    try:
        await db.execute(text("SELECT 1"))
        components.append(
            HealthComponent(
                name="database",
                status="ok",
                latency_ms=round((time.perf_counter() - started) * 1000, 2),
            )
        )
    except Exception as exc:
        components.append(
            HealthComponent(name="database", status="down", detail=type(exc).__name__)
        )

    started = time.perf_counter()
    store_ok = await store.ping()
    components.append(
        HealthComponent(
            name="session_store",
            status="ok" if store_ok else "down",
            detail=settings.session_store_backend,
            latency_ms=round((time.perf_counter() - started) * 1000, 2),
        )
    )

    components.append(await _llm_component(request, settings))

    # Later-milestone components report their configured state so the dashboard
    # and the telephony health check show the truth rather than a stub "ok".
    for name, provider in (
        ("stt", settings.stt_provider),
        ("tts", settings.tts_provider),
        ("vad", settings.vad_provider),
    ):
        components.append(
            HealthComponent(
                name=name,
                status="not_configured" if provider == "none" else "unknown",
                detail=f"provider={provider}",
            )
        )

    # Only components required to serve a text conversation gate the status.
    required = [c for c in components if c.name in ("database", "session_store")]
    if any(c.status == "down" for c in required):
        status_value = "down"
        response.status_code = 503
    else:
        status_value = "ok"

    return HealthResponse(
        status=status_value,
        version=VERSION,
        environment=settings.app_env,
        components=components,
    )


@router.get("/health/live")
async def liveness() -> dict[str, str]:
    """Process liveness only — no dependency checks."""
    return {"status": "ok"}


@router.get("/health/metrics")
async def metrics_snapshot(settings: AppSettings) -> dict[str, object]:
    """In-process metrics. Counters and latency histograms, no patient data."""
    return {"environment": settings.app_env, **metrics.snapshot()}
