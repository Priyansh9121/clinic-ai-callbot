"""Health reporting for the LLM, and the guarantee that it cannot cause an outage.

The distinction this file exists to pin down:

    LLM down  =  degraded intelligence, not a service outage

Once telephony lands, Asterisk routes calls on ``GET /health``. If a stopped
Ollama made that endpoint report ``down``, every caller would be sent to
reception over a failure the system is explicitly designed to absorb.
"""

from __future__ import annotations

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


class StubProvider:
    def __init__(
        self, *, healthy: bool = True, circuit_open: bool = False, raises: bool = False
    ) -> None:
        self._healthy = healthy
        self.circuit_open = circuit_open
        self._raises = raises

    async def health(self) -> bool:
        if self._raises:
            raise ConnectionError("runtime unreachable")
        return self._healthy

    async def analyse_message(self, *args, **kwargs):
        raise NotImplementedError

    async def aclose(self) -> None:
        return None


def _make_client(settings, sessionmaker, provider) -> httpx.AsyncClient:
    app = create_app(settings)
    app.state.session_store = InMemorySessionStore(ttl_seconds=600)
    app.state.safety_engine = SafetyEngine()
    app.state.analyser = BaselineAnalyser()
    app.state.knowledge = build_dev_knowledge()
    app.state.llm_provider = provider

    async def _db_override():
        async with sessionmaker() as session:
            yield session

    app.dependency_overrides[get_db_session] = _db_override
    app.dependency_overrides[get_session_store] = lambda: app.state.session_store
    return httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.fixture
async def client_factory(settings, sessionmaker_fixture, clinic_data):
    clients: list[httpx.AsyncClient] = []

    def _factory(*, provider=None, **overrides) -> httpx.AsyncClient:
        config = settings.model_copy(update=overrides)
        client = _make_client(config, sessionmaker_fixture, provider)
        clients.append(client)
        return client

    yield _factory
    for client in clients:
        await client.aclose()


def _llm(body: dict) -> dict:
    return next(c for c in body["components"] if c["name"] == "llm")


async def test_not_configured_when_disabled(client_factory) -> None:
    client = client_factory(llm_provider="none")
    body = (await client.get("/health")).json()
    assert _llm(body)["status"] == "not_configured"
    assert body["status"] == "ok"


async def test_ok_when_the_model_is_reachable(client_factory) -> None:
    client = client_factory(
        llm_provider="ollama",
        llm_model="qwen2.5:1.5b-instruct",
        llm_mode="llm",
        provider=StubProvider(healthy=True),
    )
    body = (await client.get("/health")).json()
    component = _llm(body)
    assert component["status"] == "ok"
    assert "provider=ollama" in component["detail"]
    assert "model=qwen2.5:1.5b-instruct" in component["detail"]


async def test_down_when_the_model_is_unreachable(client_factory) -> None:
    client = client_factory(
        llm_provider="ollama",
        llm_model="m",
        llm_mode="llm",
        provider=StubProvider(healthy=False),
    )
    body = (await client.get("/health")).json()
    assert _llm(body)["status"] == "down"


async def test_degraded_when_the_circuit_is_open(client_factory) -> None:
    client = client_factory(
        llm_provider="ollama",
        llm_model="m",
        llm_mode="llm",
        provider=StubProvider(healthy=True, circuit_open=True),
    )
    assert _llm((await client.get("/health")).json())["status"] == "degraded"


async def test_a_raising_health_check_is_reported_not_propagated(
    client_factory,
) -> None:
    client = client_factory(
        llm_provider="ollama",
        llm_model="m",
        llm_mode="llm",
        provider=StubProvider(raises=True),
    )
    response = await client.get("/health")
    assert response.status_code == 200
    assert _llm(response.json())["status"] == "down"


@pytest.mark.parametrize("provider", [StubProvider(healthy=False), None])
async def test_an_unhealthy_llm_does_not_make_the_service_unhealthy(
    client_factory, provider
) -> None:
    """The requirement that telephony fallback depends on."""
    client = client_factory(llm_provider="ollama", llm_model="m", llm_mode="llm", provider=provider)
    response = await client.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert _llm(body)["status"] == "down"


async def test_conversations_still_work_with_the_llm_down(client_factory) -> None:
    client = client_factory(
        llm_provider="ollama",
        llm_model="m",
        llm_mode="llm",
        provider=StubProvider(healthy=False),
    )
    start = (await client.post("/api/conversation/start", json={"channel": "text"})).json()
    reply = await client.post(
        "/api/conversation/message",
        json={"session_id": start["session_id"], "message": "I need an appointment"},
    )
    assert reply.status_code == 200
    assert reply.json()["intent"] == "BOOK_APPOINTMENT"


async def test_health_detail_exposes_no_prompt_or_credential(
    client_factory,
) -> None:
    client = client_factory(
        llm_provider="ollama",
        llm_model="m",
        llm_mode="llm",
        provider=StubProvider(healthy=True),
    )
    detail = _llm((await client.get("/health")).json())["detail"].lower()
    for forbidden in ("prompt", "system", "password", "token", "key", "utterance"):
        assert forbidden not in detail


async def test_metrics_endpoint_stays_free_of_caller_text(client_factory) -> None:
    client = client_factory(llm_provider="none")
    await client.post("/api/conversation/start", json={"channel": "text"})
    body = (await client.get("/health/metrics")).json()

    rendered = str(body).lower()
    for identifier in ("hughes", "margaret", "1958", "appointment tomorrow"):
        assert identifier not in rendered
