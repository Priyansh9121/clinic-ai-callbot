"""Health checks must not be a way to make the machine do expensive work.

/health/live is what the Asterisk dialplan curls before handing a caller to the
AI, so it has to stay fast and reliable. /health is unauthenticated and exempt
from rate limiting, and it used to construct the speech providers on every
request — building the VAD loads its model — which made it a way to load models
as fast as requests could arrive.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.integration


@pytest.fixture
async def client(settings, sessionmaker_fixture, clinic_data):
    """A real app instance, reusing the wiring the LLM health tests already use."""
    from tests.integration.test_llm_health import _make_client

    async with _make_client(settings, sessionmaker_fixture, None) as c:
        yield c


@pytest.fixture(autouse=True)
def _clear_cache():
    from app.api.routes.health import reset_voice_probe_cache

    reset_voice_probe_cache()
    yield
    reset_voice_probe_cache()


async def test_liveness_never_touches_the_speech_stack(client, monkeypatch) -> None:
    """A hundred liveness probes must build nothing at all."""
    from app.api.routes import health

    built: list[str] = []
    for name in ("build_stt", "build_tts", "build_vad"):
        monkeypatch.setattr(health, name, lambda _s, _n=name: built.append(_n), raising=True)

    for _ in range(100):
        response = await client.get("/health/live")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}

    assert built == [], f"liveness constructed speech providers: {built}"


async def test_repeated_diagnostics_build_the_providers_once(client, monkeypatch) -> None:
    """The probe is real, but it is not repeated per request."""
    from app.api.routes import health

    built: list[str] = []

    def _record(name):
        def probe(_settings):
            built.append(name)
            return None

        return probe

    for name in ("build_stt", "build_tts", "build_vad"):
        monkeypatch.setattr(health, name, _record(name), raising=True)

    for _ in range(20):
        assert (await client.get("/health")).status_code == 200

    assert built, "the probe never ran, so this proves nothing"
    assert len(built) <= 3, (
        f"providers were constructed {len(built)} times across 20 requests — "
        "the probe is running per request again"
    )
