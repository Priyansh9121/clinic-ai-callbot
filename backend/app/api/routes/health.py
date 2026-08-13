"""Health and metrics endpoints.

``GET /health`` is what the telephony layer calls before routing a call to the
AI. If it does not return ``ok``, Asterisk sends the caller straight to
reception (see docs/telephony/fallback.md) — so this endpoint must be cheap,
must never block, and must reflect the components a call actually needs.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy import text

from app.api.deps import AppSettings, DbSession, get_session_store
from app.conversation.store import SessionStore
from app.core.metrics import metrics
from app.schemas.common import HealthComponent, HealthResponse
from app.speech.factory import build_stt, build_tts, build_vad

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


async def _voice_components(settings: AppSettings) -> list[HealthComponent]:
    """Per-component voice status: stt, tts, vad, audio_input, audio_output.

    Reported separately because they fail separately and mean different things:
    a missing TTS voice means the agent can hear but not answer, while a missing
    sound card means only that *this host* cannot run the local demo — the
    telephony transport in Milestone 4 will not use one.

    None of these gate the overall status.
    """
    components: list[HealthComponent] = []

    for name, provider, probe in (
        ("stt", settings.stt_provider, build_stt),
        ("tts", settings.tts_provider, build_tts),
        ("vad", settings.vad_provider, build_vad),
    ):
        if provider == "none":
            components.append(
                HealthComponent(name=name, status="not_configured", detail="provider=none")
            )
            continue
        detail = f"provider={provider}"
        if name == "stt":
            detail += f" model={settings.stt_model}"
        elif name == "tts":
            detail += f" voice={Path(settings.tts_voice).stem}"
        try:
            instance = probe(settings)
            if instance is None:
                components.append(
                    HealthComponent(name=name, status="down", detail=f"{detail} unavailable")
                )
                continue
            # VAD has no health(): constructing it already loads the model, so
            # a successful build is the check.
            healthy = True
            checker = getattr(instance, "health", None)
            if checker is not None:
                healthy = await checker()
            components.append(
                HealthComponent(name=name, status="ok" if healthy else "down", detail=detail)
            )
        except Exception as exc:
            components.append(
                HealthComponent(
                    name=name, status="down", detail=f"{detail} error={type(exc).__name__}"
                )
            )

    components.extend(_audio_device_components(settings))
    return components


async def _telephony_components(request: Request, settings: AppSettings) -> list[HealthComponent]:
    """Per-component telephony status: pbx, media, capacity.

    Split the same way as voice, because the failure modes differ: the PBX
    being unreachable stops new calls arriving, while the media server not
    listening means calls arrive and then hear nothing.
    """
    if not settings.telephony_enabled:
        return [HealthComponent(name="telephony", status="not_configured", detail="disabled")]
    if not settings.telephony_configured:
        return [
            HealthComponent(
                name="telephony",
                status="not_configured",
                detail="voice providers or ARI credentials missing",
            )
        ]

    components: list[HealthComponent] = []
    service = getattr(request.app.state, "telephony", None)
    if service is None:
        return [HealthComponent(name="telephony", status="down", detail="service not started")]

    ari = getattr(service, "_ari", None)
    connected = bool(getattr(ari, "is_connected", False))
    components.append(
        HealthComponent(
            name="telephony_pbx",
            status="ok" if connected else "down",
            detail=f"ari app={settings.asterisk_app_name}",
        )
    )
    components.append(
        HealthComponent(
            name="telephony_media",
            status="ok" if service.is_listening else "down",
            detail=f"audiosocket {settings.telephony_wire_format} port={service.media_port}",
        )
    )
    manager = service.manager
    components.append(
        HealthComponent(
            name="telephony_capacity",
            status="ok" if not manager.at_capacity else "degraded",
            detail=f"{manager.active_calls}/{manager.max_active_calls} active",
        )
    )
    return components


def _audio_device_components(settings: AppSettings) -> list[HealthComponent]:
    """Local sound-card availability. Only meaningful for the local demo."""
    if not settings.voice_configured:
        return [
            HealthComponent(name="audio_input", status="not_configured", detail="voice disabled"),
            HealthComponent(name="audio_output", status="not_configured", detail="voice disabled"),
        ]
    try:
        from app.voice.devices import list_devices

        devices = list_devices()
    except Exception as exc:
        detail = f"error={type(exc).__name__}"
        return [
            HealthComponent(name="audio_input", status="down", detail=detail),
            HealthComponent(name="audio_output", status="down", detail=detail),
        ]

    inputs = sum(1 for d in devices if d["inputs"] > 0)
    outputs = sum(1 for d in devices if d["outputs"] > 0)
    return [
        HealthComponent(
            name="audio_input",
            status="ok" if inputs else "down",
            detail=f"devices={inputs}",
        ),
        HealthComponent(
            name="audio_output",
            status="ok" if outputs else "down",
            detail=f"devices={outputs}",
        ),
    ]


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
    components.extend(await _voice_components(settings))
    components.extend(await _telephony_components(request, settings))

    # Only components required to serve a text conversation gate the status.
    # The LLM, the voice stack and telephony are deliberately excluded: losing
    # them degrades capability, it does not take the clinic off the air.
    #
    # Telephony in particular must not gate this endpoint, because the dialplan
    # calls it to decide whether to route a caller to the AI at all. Reporting
    # "down" because the PBX is unreachable would be circular, and reporting
    # 503 for a text deployment with no PBX would send every caller to
    # reception forever.
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
