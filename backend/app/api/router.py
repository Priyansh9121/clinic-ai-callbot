"""API router assembly."""

from __future__ import annotations

from fastapi import APIRouter

from app.api.routes import appointments, availability, clinic, conversation, health

# Health lives at the root: /health, not /api/health, so infrastructure probes
# do not depend on the API prefix.
health_router = APIRouter()
health_router.include_router(health.router)

api_router = APIRouter(prefix="/api")
api_router.include_router(clinic.router)
api_router.include_router(availability.router)
api_router.include_router(appointments.router)
api_router.include_router(conversation.router)
