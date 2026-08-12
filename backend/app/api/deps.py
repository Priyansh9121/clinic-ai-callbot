"""FastAPI dependency wiring.

Routes depend on *services*, never on a raw session plus ad-hoc queries. This
keeps business logic out of the routing layer and gives one place to change how
a component is constructed.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.appointments.availability import AvailabilityService
from app.appointments.service import AppointmentService
from app.clinic.knowledge import ClinicKnowledgeProvider
from app.clinic.service import ClinicService
from app.conversation.engine import ConversationEngine
from app.conversation.nlu.baseline import MessageAnalyser
from app.conversation.store import SessionStore
from app.core.config import Settings
from app.database.session import get_db_session
from app.safety.engine import SafetyEngine


def get_app_settings(request: Request) -> Settings:
    """The settings this application instance was created with.

    Resolved from application state rather than the process-wide cache, so
    ``create_app(settings)`` is genuinely authoritative — that is what lets a
    test (or a second instance in one process) run with its own configuration
    instead of silently picking up the ambient .env.
    """
    return request.app.state.settings


DbSession = Annotated[AsyncSession, Depends(get_db_session)]
AppSettings = Annotated[Settings, Depends(get_app_settings)]


# --- Application-scoped singletons (created during lifespan) ---------------
def get_session_store(request: Request) -> SessionStore:
    return request.app.state.session_store


def get_safety_engine(request: Request) -> SafetyEngine:
    return request.app.state.safety_engine


def get_analyser(request: Request) -> MessageAnalyser:
    return request.app.state.analyser


def get_knowledge(request: Request) -> ClinicKnowledgeProvider:
    return request.app.state.knowledge


# --- Request-scoped services -----------------------------------------------
def get_clinic_service(db: DbSession) -> ClinicService:
    return ClinicService(db)


def get_availability_service(db: DbSession, settings: AppSettings) -> AvailabilityService:
    return AvailabilityService(db, settings)


def get_appointment_service(db: DbSession, settings: AppSettings) -> AppointmentService:
    return AppointmentService(db, settings)


def get_conversation_engine(
    db: DbSession,
    settings: AppSettings,
    store: Annotated[SessionStore, Depends(get_session_store)],
    safety: Annotated[SafetyEngine, Depends(get_safety_engine)],
    analyser: Annotated[MessageAnalyser, Depends(get_analyser)],
    knowledge: Annotated[ClinicKnowledgeProvider, Depends(get_knowledge)],
) -> ConversationEngine:
    return ConversationEngine(
        db=db,
        settings=settings,
        store=store,
        safety=safety,
        analyser=analyser,
        knowledge=knowledge,
    )


ClinicSvc = Annotated[ClinicService, Depends(get_clinic_service)]
AvailabilitySvc = Annotated[AvailabilityService, Depends(get_availability_service)]
AppointmentSvc = Annotated[AppointmentService, Depends(get_appointment_service)]
Engine = Annotated[ConversationEngine, Depends(get_conversation_engine)]
