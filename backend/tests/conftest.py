"""Shared test fixtures.

Integration tests run against a real PostgreSQL database, because the
behaviour that matters most — row locking, partial unique indexes, transaction
boundaries — does not exist in SQLite. Each test gets a schema created and
dropped around it, so tests never see each other's data.

Set ``TEST_DATABASE_URL`` to point at a scratch database. It defaults to
``clinic_callbot_test`` on the project's development cluster, which
``scripts/dev_services.sh init`` creates.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.clinic.knowledge_data import build_dev_knowledge
from app.conversation.engine import ConversationEngine
from app.conversation.nlu.baseline import BaselineAnalyser
from app.conversation.store import InMemorySessionStore
from app.conversation.vocabulary import clear_cache
from app.core.config import Settings
from app.database.base import Base
from app.database.models.appointment_type import AppointmentType
from app.database.models.availability import AvailabilitySlot
from app.database.models.clinic import Clinic, Location
from app.database.models.enums import SlotStatus
from app.database.models.patient import Patient
from app.database.models.practitioner import Practitioner
from app.safety.engine import SafetyEngine

DEFAULT_TEST_DSN = "postgresql+asyncpg://clinic_app:clinic_app@127.0.0.1:5433/clinic_callbot_test"
TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL", DEFAULT_TEST_DSN)
MELBOURNE = ZoneInfo("Australia/Melbourne")


@pytest.fixture(scope="session")
def settings() -> Settings:
    return Settings(
        app_env="test",
        database_url=TEST_DATABASE_URL,
        redis_url="",  # in-memory session store for tests
        app_secret_key="test-secret-key-not-used-in-production-000000",
        booking_lead_minutes=60,
        max_slot_offers=3,
        max_misunderstandings=2,
        availability_search_days=14,
        rate_limit_enabled=False,
    )


@pytest.fixture(scope="session")
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
async def engine_fixture(settings: Settings) -> AsyncIterator:
    engine = create_async_engine(settings.database_url, poolclass=None)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)
        await connection.run_sync(Base.metadata.create_all)
    yield engine
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@pytest.fixture
async def sessionmaker_fixture(engine_fixture) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(bind=engine_fixture, expire_on_commit=False, autoflush=False)


@pytest.fixture
async def db(sessionmaker_fixture) -> AsyncIterator[AsyncSession]:
    async with sessionmaker_fixture() as session:
        yield session


# --- Test clinic ------------------------------------------------------------
class ClinicFixture:
    """Handles to the seeded records, so tests read clearly."""

    def __init__(
        self,
        clinic: Clinic,
        location: Location,
        practitioners: dict[str, Practitioner],
        types: dict[str, AppointmentType],
        patients: dict[str, Patient],
    ) -> None:
        self.clinic = clinic
        self.location = location
        self.practitioners = practitioners
        self.types = types
        self.patients = patients

    @property
    def tz(self) -> ZoneInfo:
        return MELBOURNE


@pytest.fixture
async def clinic_data(db: AsyncSession) -> ClinicFixture:
    """A small, fully deterministic clinic.

    Unlike the development seed this creates a known, exact set of slots, so a
    test can assert "exactly one booking succeeded" without ambiguity.
    """
    clear_cache()
    clinic = Clinic(name="Test Clinic", phone="+61390000000", timezone="Australia/Melbourne")
    db.add(clinic)
    await db.flush()

    location = Location(clinic_id=clinic.id, name="Main", address="1 Test St")
    db.add(location)
    await db.flush()

    types = {
        "standard_gp": AppointmentType(
            clinic_id=clinic.id,
            slug="standard_gp",
            name="Standard GP",
            duration_minutes=15,
            new_patient_allowed=True,
        ),
        "long_gp": AppointmentType(
            clinic_id=clinic.id,
            slug="long_gp",
            name="Long GP",
            duration_minutes=30,
            new_patient_allowed=True,
        ),
        "new_patient": AppointmentType(
            clinic_id=clinic.id,
            slug="new_patient",
            name="New Patient",
            duration_minutes=30,
            new_patient_allowed=True,
        ),
        "telehealth": AppointmentType(
            clinic_id=clinic.id,
            slug="telehealth",
            name="Telehealth",
            duration_minutes=15,
            new_patient_allowed=False,
            telehealth_allowed=True,
        ),
    }
    db.add_all(types.values())
    await db.flush()

    practitioners = {
        "patel": Practitioner(
            clinic_id=clinic.id,
            location_id=location.id,
            first_name="Sarah",
            last_name="Patel",
            display_name="Dr Sarah Patel",
            speciality="General Practice",
        ),
        "wilson": Practitioner(
            clinic_id=clinic.id,
            location_id=location.id,
            first_name="James",
            last_name="Wilson",
            display_name="Dr James Wilson",
            speciality="General Practice",
        ),
        "chen": Practitioner(
            clinic_id=clinic.id,
            location_id=location.id,
            first_name="Emily",
            last_name="Chen",
            display_name="Dr Emily Chen",
            accepts_new_patients=False,
        ),
    }
    db.add_all(practitioners.values())
    await db.flush()

    patients = {
        "hughes": Patient(
            clinic_id=clinic.id,
            first_name="Margaret",
            last_name="Hughes",
            date_of_birth=date(1958, 3, 14),
            mobile="0491570156",
            mobile_normalised="+61491570156",
        ),
        "nguyen": Patient(
            clinic_id=clinic.id,
            first_name="David",
            last_name="Nguyen",
            date_of_birth=date(1982, 11, 2),
            mobile="0491570157",
            mobile_normalised="+61491570157",
        ),
    }
    db.add_all(patients.values())
    await db.flush()

    # Availability: the next 14 days, 9am-4pm, every practitioner, standard and
    # long appointments. Generated in clinic-local time then stored as UTC.
    today = datetime.now(MELBOURNE).date()
    slots: list[AvailabilitySlot] = []
    for offset in range(1, 15):
        day = today + timedelta(days=offset)
        if day.weekday() > 4:  # weekdays only
            continue
        for practitioner in practitioners.values():
            for hour in range(9, 16):
                for appointment_type, minute in (
                    (types["standard_gp"], 0),
                    (types["standard_gp"], 30),
                ):
                    start = datetime.combine(day, time(hour, minute), tzinfo=MELBOURNE)
                    slots.append(
                        AvailabilitySlot(
                            id=uuid.uuid4(),
                            practitioner_id=practitioner.id,
                            location_id=location.id,
                            appointment_type_id=appointment_type.id,
                            start_time=start.astimezone(ZoneInfo("UTC")),
                            end_time=(start + timedelta(minutes=15)).astimezone(ZoneInfo("UTC")),
                            status=SlotStatus.AVAILABLE,
                        )
                    )
            # One block per day for each of the non-standard types.
            start = datetime.combine(day, time(16, 0), tzinfo=MELBOURNE)
            for appointment_type in (
                types["long_gp"],
                types["new_patient"],
                types["telehealth"],
            ):
                slots.append(
                    AvailabilitySlot(
                        id=uuid.uuid4(),
                        practitioner_id=practitioner.id,
                        location_id=location.id,
                        appointment_type_id=appointment_type.id,
                        start_time=start.astimezone(ZoneInfo("UTC")),
                        end_time=(start + timedelta(minutes=30)).astimezone(ZoneInfo("UTC")),
                        status=SlotStatus.AVAILABLE,
                    )
                )
                start += timedelta(minutes=30)

    db.add_all(slots)
    await db.commit()
    clear_cache()
    return ClinicFixture(clinic, location, practitioners, types, patients)


@pytest.fixture
def session_store() -> InMemorySessionStore:
    return InMemorySessionStore(ttl_seconds=600)


@pytest.fixture
def conversation_engine(
    db: AsyncSession, settings: Settings, session_store: InMemorySessionStore
) -> ConversationEngine:
    return ConversationEngine(
        db=db,
        settings=settings,
        store=session_store,
        safety=SafetyEngine(),
        analyser=BaselineAnalyser(),
        knowledge=build_dev_knowledge(),
    )


class Caller:
    """Drives a conversation and records it, for readable test assertions."""

    def __init__(self, engine: ConversationEngine) -> None:
        self._engine = engine
        self.session_id: str = ""
        self.transcript: list[tuple[str, str]] = []
        self.last = None

    async def start(self) -> str:
        result = await self._engine.start(channel="text")
        self.session_id = result.session_id
        self.last = result
        self.transcript.append(("bot", result.reply))
        return result.reply

    async def say(self, message: str) -> str:
        if not self.session_id:
            await self.start()
        self.transcript.append(("caller", message))
        result = await self._engine.handle_message(self.session_id, message)
        self.last = result
        self.transcript.append(("bot", result.reply))
        return result.reply

    async def session(self):
        return await self._engine.get_session(self.session_id)

    def dump(self) -> str:
        return "\n".join(f"{who:>6}: {text}" for who, text in self.transcript)


@pytest.fixture
async def caller(conversation_engine: ConversationEngine, clinic_data: ClinicFixture) -> Caller:
    caller = Caller(conversation_engine)
    await caller.start()
    return caller
