"""HTTP API tests.

Exercises the real application through ASGI, including middleware, so header
and authorisation behaviour is covered rather than assumed.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest
from httpx import ASGITransport

from app.api.deps import get_session_store
from app.clinic.knowledge_data import build_dev_knowledge
from app.conversation.nlu.baseline import BaselineAnalyser
from app.conversation.store import InMemorySessionStore
from app.database.session import get_db_session
from app.main import create_app
from app.safety.engine import SafetyEngine

pytestmark = pytest.mark.integration

STAFF_KEY = "test-staff-key"
ADMIN_KEY = "test-admin-key"


@pytest.fixture
async def client(sessionmaker_fixture, settings, clinic_data) -> AsyncIterator[httpx.AsyncClient]:
    api_settings = settings.model_copy(
        update={
            "staff_api_keys": f"{STAFF_KEY}:staff,{ADMIN_KEY}:admin",
            "cors_allowed_origins": "http://localhost:5173",
        }
    )
    app = create_app(api_settings)

    # Application-scoped singletons normally created in lifespan.
    app.state.session_store = InMemorySessionStore(ttl_seconds=600)
    app.state.safety_engine = SafetyEngine()
    app.state.analyser = BaselineAnalyser()
    app.state.knowledge = build_dev_knowledge()

    async def _db_override():
        async with sessionmaker_fixture() as session:
            yield session

    app.dependency_overrides[get_db_session] = _db_override
    app.dependency_overrides[get_session_store] = lambda: app.state.session_store

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as http_client:
        yield http_client


# --- Health -----------------------------------------------------------------
async def test_health_reports_components(client: httpx.AsyncClient) -> None:
    response = await client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    names = {c["name"] for c in body["components"]}
    assert {"database", "session_store", "llm", "stt", "tts"} <= names


async def test_liveness_does_not_touch_dependencies(client: httpx.AsyncClient) -> None:
    assert (await client.get("/health/live")).json() == {"status": "ok"}


async def test_metrics_endpoint_exposes_no_patient_data(client: httpx.AsyncClient) -> None:
    await client.post("/api/conversation/start", json={"channel": "text"})
    body = (await client.get("/health/metrics")).json()
    rendered = str(body).lower()
    for identifier in ("hughes", "margaret", "nguyen", "1958"):
        assert identifier not in rendered


# --- Security headers -------------------------------------------------------
async def test_security_headers_are_applied(client: httpx.AsyncClient) -> None:
    headers = (await client.get("/health")).headers
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert headers["X-Frame-Options"] == "DENY"
    assert headers["Referrer-Policy"] == "no-referrer"
    assert "Content-Security-Policy" in headers
    assert headers["Cache-Control"] == "no-store"
    assert "X-Request-ID" in headers


# --- Clinic directory -------------------------------------------------------
async def test_clinic_endpoints_are_readable(client: httpx.AsyncClient) -> None:
    clinic = (await client.get("/api/clinic")).json()
    assert clinic["name"] == "Test Clinic"
    assert clinic["timezone"] == "Australia/Melbourne"

    practitioners = (await client.get("/api/practitioners")).json()
    assert len(practitioners) == 3
    # Response models expose only approved fields.
    assert set(practitioners[0]) == {
        "id",
        "display_name",
        "speciality",
        "accepts_new_patients",
        "location_id",
    }

    types = (await client.get("/api/appointment-types")).json()
    assert {t["slug"] for t in types} >= {"standard_gp", "long_gp", "new_patient"}


async def test_availability_search(client: httpx.AsyncClient) -> None:
    slots = (await client.get("/api/availability", params={"limit": 5})).json()
    assert 0 < len(slots) <= 5
    assert set(slots[0]) == {
        "id",
        "start_time",
        "end_time",
        "practitioner_id",
        "location_id",
        "appointment_type_id",
    }


async def test_availability_rejects_invalid_parameters(client: httpx.AsyncClient) -> None:
    assert (await client.get("/api/availability", params={"limit": 5000})).status_code == 422
    assert (await client.get("/api/availability", params={"period": "NIGHT"})).status_code == 422
    assert (
        await client.get("/api/availability", params={"practitioner_id": "not-a-uuid"})
    ).status_code == 422


# --- Authorisation ----------------------------------------------------------
async def test_booking_requires_authentication(client: httpx.AsyncClient) -> None:
    slots = (await client.get("/api/availability", params={"limit": 1})).json()
    response = await client.post(
        "/api/appointments",
        json={"patient_id": "00000000-0000-0000-0000-000000000000", "slot_id": slots[0]["id"]},
    )
    assert response.status_code == 401
    assert response.json()["code"] == "unauthenticated"


async def test_an_invalid_api_key_is_refused(client: httpx.AsyncClient) -> None:
    slots = (await client.get("/api/availability", params={"limit": 1})).json()
    response = await client.post(
        "/api/appointments",
        json={"patient_id": "00000000-0000-0000-0000-000000000000", "slot_id": slots[0]["id"]},
        headers={"X-API-Key": "wrong-key"},
    )
    assert response.status_code == 401


async def test_staff_can_book_cancel_and_reschedule(client: httpx.AsyncClient, clinic_data) -> None:
    slots = (await client.get("/api/availability", params={"limit": 20})).json()
    patient_id = str(clinic_data.patients["hughes"].id)
    auth = {"X-API-Key": STAFF_KEY}

    created = await client.post(
        "/api/appointments",
        json={"patient_id": patient_id, "slot_id": slots[0]["id"], "booking_source": "RECEPTION"},
        headers=auth,
    )
    assert created.status_code == 201, created.text
    appointment = created.json()
    # Patient details are minimised in responses.
    assert set(appointment["patient"]) == {"id", "first_name"}
    assert "date_of_birth" not in appointment["patient"]
    assert "mobile" not in appointment["patient"]

    other = next(s for s in slots if s["start_time"] != slots[0]["start_time"])
    moved = await client.post(
        f"/api/appointments/{appointment['id']}/reschedule",
        json={"new_slot_id": other["id"]},
        headers=auth,
    )
    assert moved.status_code == 200, moved.text
    assert moved.json()["id"] != appointment["id"]

    cancelled = await client.post(
        f"/api/appointments/{moved.json()['id']}/cancel",
        json={"reason": "test"},
        headers=auth,
    )
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "CANCELLED"


async def test_double_booking_returns_a_conflict(client: httpx.AsyncClient, clinic_data) -> None:
    slots = (await client.get("/api/availability", params={"limit": 1})).json()
    auth = {"X-API-Key": STAFF_KEY}
    payload = {
        "patient_id": str(clinic_data.patients["hughes"].id),
        "slot_id": slots[0]["id"],
    }
    assert (await client.post("/api/appointments", json=payload, headers=auth)).status_code == 201

    payload["patient_id"] = str(clinic_data.patients["nguyen"].id)
    conflict = await client.post("/api/appointments", json=payload, headers=auth)
    assert conflict.status_code == 409
    assert conflict.json()["code"] in ("slot_unavailable", "duplicate_booking")


async def test_idempotent_booking_over_http(client: httpx.AsyncClient, clinic_data) -> None:
    slots = (await client.get("/api/availability", params={"limit": 1})).json()
    auth = {"X-API-Key": STAFF_KEY}
    payload = {
        "patient_id": str(clinic_data.patients["hughes"].id),
        "slot_id": slots[0]["id"],
        "idempotency_key": "http-retry-key-1",
    }
    first = await client.post("/api/appointments", json=payload, headers=auth)
    second = await client.post("/api/appointments", json=payload, headers=auth)
    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json()["id"] == second.json()["id"]


async def test_unknown_appointment_returns_a_generic_not_found(
    client: httpx.AsyncClient,
) -> None:
    response = await client.get(
        "/api/appointments/11111111-1111-1111-1111-111111111111",
        headers={"X-API-Key": STAFF_KEY},
    )
    assert response.status_code == 404
    # No internal detail in the public message.
    assert "sql" not in response.text.lower()
    assert "traceback" not in response.text.lower()


# --- Conversation -----------------------------------------------------------
async def test_conversation_endpoint_round_trip(client: httpx.AsyncClient) -> None:
    start = (await client.post("/api/conversation/start", json={"channel": "text"})).json()
    session_id = start["session_id"]
    assert "automated" in start["reply"].lower()

    response = await client.post(
        "/api/conversation/message",
        json={"session_id": session_id, "message": "I need an appointment next Friday"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["intent"] == "BOOK_APPOINTMENT"
    assert body["requires_human"] is False
    assert body["emergency"] is False
    assert set(body) >= {"session_id", "reply", "state", "intent", "requires_human", "emergency"}


async def test_conversation_emergency_is_reported_in_the_response(
    client: httpx.AsyncClient,
) -> None:
    start = (await client.post("/api/conversation/start", json={"channel": "text"})).json()
    body = (
        await client.post(
            "/api/conversation/message",
            json={"session_id": start["session_id"], "message": "I have chest pain"},
        )
    ).json()
    assert body["emergency"] is True
    assert body["state"] == "EMERGENCY"
    assert "000" in body["reply"]


async def test_conversation_rejects_invalid_payloads(client: httpx.AsyncClient) -> None:
    assert (
        await client.post("/api/conversation/message", json={"session_id": "x", "message": "hi"})
    ).status_code == 422
    assert (
        await client.post(
            "/api/conversation/message", json={"session_id": "sess_abcdef", "message": ""}
        )
    ).status_code == 422


async def test_validation_errors_do_not_echo_submitted_values(
    client: httpx.AsyncClient,
) -> None:
    """A rejected field must not be reflected back — it may hold patient data."""
    response = await client.post(
        "/api/conversation/message",
        json={"session_id": "sess_abcdef", "message": "x" * 5000},
    )
    assert response.status_code == 422
    assert "xxxxx" not in response.text
