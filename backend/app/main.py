"""Application factory.

Deliberately thin: wiring, middleware and error translation only. All behaviour
lives in the domain packages.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.router import api_router, health_router
from app.clinic.knowledge_data import build_dev_knowledge
from app.conversation.store import build_session_store
from app.core.config import Settings, get_settings
from app.core.errors import AppError
from app.core.logging import configure_logging, get_logger
from app.database.session import dispose_engine
from app.llm.factory import build_llm_provider, build_message_analyser
from app.safety.engine import SafetyEngine
from app.security.headers import RequestContextMiddleware, SecurityHeadersMiddleware
from app.security.ratelimit import RateLimitMiddleware
from app.telephony.runtime import start_telephony, stop_telephony

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = app.state.settings

    # Application-scoped singletons. Stateless and safe to share across
    # requests; per-request services are built in app.api.deps.
    app.state.session_store = build_session_store(settings.redis_url, settings.session_ttl_seconds)
    app.state.safety_engine = SafetyEngine()
    app.state.knowledge = build_dev_knowledge()

    # The analyser is chosen by configuration. Nothing downstream knows whether
    # a model is involved — routes and the engine only ever see a
    # MessageAnalyser. Construction touches no network, so an unreachable model
    # cannot stop the backend from starting; it is discovered on the first turn
    # and handled by the deterministic fallback.
    app.state.llm_provider = build_llm_provider(settings)
    app.state.analyser = build_message_analyser(settings, provider=app.state.llm_provider)

    logger.info(
        "app.startup",
        environment=settings.app_env,
        session_store=settings.session_store_backend,
        llm_provider=settings.llm_provider,
        llm_mode=settings.effective_llm_mode,
    )

    # Warm the model so the first caller does not pay for loading weights.
    # Backgrounded and never awaited at startup: a slow or dead runtime must
    # not delay the clinic becoming reachable.
    if app.state.llm_provider is not None and settings.effective_llm_mode != "baseline":
        warmup = getattr(app.state.llm_provider, "warmup", None)
        if warmup is not None:
            app.state.llm_warmup_task = asyncio.create_task(warmup())

    # Telephony last, because it depends on the analyser and session store
    # above. It declines rather than raises when unconfigured or unreachable:
    # a PBX problem must not stop the clinic answering text and local voice,
    # and the dialplan's health-check failover routes callers to reception
    # while /health reports telephony down.
    app.state.telephony = await start_telephony(app.state, settings)

    try:
        yield
    finally:
        await stop_telephony(getattr(app.state, "telephony", None))
        task = getattr(app.state, "llm_warmup_task", None)
        if task is not None and not task.done():
            task.cancel()
        provider = getattr(app.state, "llm_provider", None)
        if provider is not None and hasattr(provider, "aclose"):
            await provider.aclose()
        await app.state.session_store.close()
        await dispose_engine()
        logger.info("app.shutdown")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings)

    app = FastAPI(
        title="Clinic AI Callbot",
        version="0.1.0",
        description=(
            "Self-hosted AI clinic receptionist. Appointment, conversation and safety APIs."
        ),
        lifespan=lifespan,
        # Interactive docs are a development convenience, not a production
        # surface.
        docs_url="/docs" if not settings.is_production else None,
        redoc_url=None,
        openapi_url="/openapi.json" if not settings.is_production else None,
    )
    app.state.settings = settings

    # Order matters: outermost first. Request context wraps everything so a
    # rejected request is still logged with its id.
    app.add_middleware(RequestContextMiddleware)
    app.add_middleware(SecurityHeadersMiddleware, settings=settings)
    app.add_middleware(RateLimitMiddleware, settings=settings)
    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origins,
            allow_credentials=True,
            allow_methods=["GET", "POST", "OPTIONS"],
            allow_headers=["Content-Type", "X-API-Key", "X-Request-ID"],
            max_age=600,
        )

    app.include_router(health_router)
    app.include_router(api_router)
    _register_error_handlers(app)
    return app


def _register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _app_error(_: Request, exc: AppError) -> JSONResponse:
        # Only the public message crosses the boundary; `detail` stays in logs.
        logger.info("api.domain_error", code=exc.code, status=exc.http_status)
        return JSONResponse(
            status_code=exc.http_status,
            content={"code": exc.code, "message": exc.public_message},
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        # Field locations and messages only — never the submitted values, which
        # may contain patient details.
        details = [
            {"field": ".".join(str(p) for p in err.get("loc", ())), "error": err.get("type", "")}
            for err in exc.errors()
        ]
        return JSONResponse(
            status_code=422,
            content={
                "code": "validation_error",
                "message": "The request was not valid.",
                "details": details,
            },
        )

    @app.exception_handler(Exception)
    async def _unhandled(_: Request, exc: Exception) -> JSONResponse:
        logger.exception("api.unhandled_error", error=type(exc).__name__)
        return JSONResponse(
            status_code=500,
            content={"code": "internal_error", "message": "Something went wrong."},
        )


app = create_app()
