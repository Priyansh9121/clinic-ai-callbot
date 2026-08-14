"""ARI call setup, including the event orderings a live PBX produces.

The loopback media tests never touch ARI, so this file covers the other half:
what happens when Asterisk's WebSocket events and our REST responses arrive in
an order we did not assume.

`FakeAriClient` stands in for Asterisk's control plane. It is a double, not a
simulator — but it can be driven into orderings that are hard to reproduce on
demand against a real PBX, which is exactly what makes them dangerous.
"""

from __future__ import annotations

import asyncio

import pytest

from app.telephony.destinations import DestinationRegistry, TransferDestination
from app.telephony.manager import CallManager
from app.telephony.service import TelephonyService

pytestmark = pytest.mark.integration


class FakeAriClient:
    """Records ARI operations and lets a test control their timing."""

    def __init__(self) -> None:
        self.answered: list[str] = []
        self.hangups: list[str] = []
        self.bridges: list[str] = []
        self.bridge_joins: list[tuple[str, str]] = []
        self.continues: list[tuple[str, str, str]] = []
        self.originated: list[str] = []
        self._handler = None
        #: When set, originate_media_channel waits on it before returning, so a
        #: test can deliver the media channel's StasisStart first.
        self.originate_gate: asyncio.Event | None = None
        self.media_channel_id = "media-chan-1"
        self.app_args_used: list[str] = []

    def on_event(self, handler: object) -> None:
        self._handler = handler

    async def start(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def answer(self, channel_id: str) -> None:
        self.answered.append(channel_id)

    async def hangup(self, channel_id: str) -> None:
        self.hangups.append(channel_id)

    async def create_bridge(self, bridge_id: str) -> None:
        self.bridges.append(bridge_id)

    async def destroy_bridge(self, bridge_id: str) -> None:
        return None

    async def add_to_bridge(self, bridge_id: str, channel_id: str) -> None:
        self.bridge_joins.append((bridge_id, channel_id))

    async def continue_in_dialplan(
        self, channel_id: str, *, context: str, extension: str, priority: int = 1
    ) -> None:
        self.continues.append((channel_id, context, extension))

    async def originate_media_channel(
        self, *, host: str, port: int, media_uuid: str, app_args: str = "media"
    ) -> str:
        self.app_args_used.append(app_args)
        self.originated.append(media_uuid)
        if self.originate_gate is not None:
            await self.originate_gate.wait()
        return self.media_channel_id

    async def deliver(self, event: dict) -> None:
        assert self._handler is not None
        await self._handler(event)


def caller_start(channel_id: str = "caller-1") -> dict:
    return {
        "type": "StasisStart",
        "args": [],
        "channel": {
            "id": channel_id,
            "caller": {"number": "0491570156", "name": "Test"},
            "dialplan": {"exten": "500"},
        },
    }


def media_start(channel_id: str, media_uuid: str) -> dict:
    """What Asterisk sends when our originated media channel enters Stasis."""
    return {
        "type": "StasisStart",
        "args": ["media", media_uuid],
        "channel": {"id": channel_id, "caller": {}, "dialplan": {"exten": ""}},
    }


@pytest.fixture
def destinations() -> DestinationRegistry:
    return DestinationRegistry(
        {TransferDestination.RECEPTION: "100", TransferDestination.VOICEMAIL: "101"},
        context="clinic-transfer",
    )


def build_service(ari: FakeAriClient, destinations: DestinationRegistry) -> TelephonyService:
    service = TelephonyService(
        ari=ari,  # type: ignore[arg-type]
        media_host="127.0.0.1",
        media_port=18090,
        destinations=destinations,
        voice_factory=lambda *_: None,
        manager=CallManager(max_active_calls=2),
    )
    ari.on_event(service._handle_event)
    return service


async def test_the_media_channel_joins_the_bridge(destinations) -> None:
    """The ordinary case: originate returns, then its StasisStart arrives."""
    ari = FakeAriClient()
    build_service(ari, destinations)

    await ari.deliver(caller_start())
    media_uuid = ari.originated[0]
    await ari.deliver(media_start(ari.media_channel_id, media_uuid))

    assert ari.answered == ["caller-1"]
    assert ari.bridges, "no bridge was created"
    bridge = ari.bridges[0]
    assert (bridge, "caller-1") in ari.bridge_joins
    assert (bridge, ari.media_channel_id) in ari.bridge_joins, (
        "the media channel never joined the bridge — the caller would hear silence"
    )


async def test_the_media_stasis_start_may_arrive_before_originate_returns(
    destinations,
) -> None:
    """The race a live PBX produces, and loopback tests cannot.

    ``POST /channels`` and the ARI WebSocket are independent connections.
    Asterisk can create the channel, emit ``StasisStart`` and have us dispatch
    it *before* the HTTP response carrying the new channel id comes back.

    Correlating on that response is therefore unsafe: the event would arrive
    while the correlation table was still empty, the media channel would never
    join the bridge, and the call would connect to silence — intermittently,
    which is the worst way to find out.
    """
    ari = FakeAriClient()
    ari.originate_gate = asyncio.Event()
    build_service(ari, destinations)

    caller = asyncio.create_task(ari.deliver(caller_start()))
    await asyncio.sleep(0.05)  # setup is now parked inside originate
    assert ari.originated, "originate was never reached"
    media_uuid = ari.originated[0]

    # The media channel enters Stasis before the POST response returns.
    await ari.deliver(media_start(ari.media_channel_id, media_uuid))
    ari.originate_gate.set()
    await asyncio.wait_for(caller, timeout=5.0)
    await asyncio.sleep(0.05)

    bridge = ari.bridges[0]
    assert (bridge, ari.media_channel_id) in ari.bridge_joins, (
        "media channel lost to the event/response race — caller would hear silence"
    )


async def test_the_media_uuid_travels_in_the_stasis_arguments(destinations) -> None:
    """Correlation must come from the event, not from a lookup table.

    Passing the UUID through appArgs is what makes the ordering above
    irrelevant: whichever arrives first, the event carries what it needs.
    """
    ari = FakeAriClient()
    build_service(ari, destinations)

    await ari.deliver(caller_start())

    assert ari.app_args_used, "no media channel was originated"
    args = ari.app_args_used[0]
    assert args.startswith("media,"), f"appArgs must carry the media UUID, got {args!r}"
    assert args.split(",", 1)[1] == ari.originated[0]


async def test_a_media_start_for_an_unknown_call_is_ignored(destinations) -> None:
    """A stale or duplicated event must not raise inside the event loop."""
    ari = FakeAriClient()
    build_service(ari, destinations)

    await ari.deliver(media_start("ghost-channel", "not-a-real-uuid"))

    assert ari.bridge_joins == []


async def test_a_second_call_at_capacity_is_refused_without_answering(
    destinations,
) -> None:
    """At capacity we decline, so the dialplan's failover reaches reception."""
    ari = FakeAriClient()
    service = TelephonyService(
        ari=ari,  # type: ignore[arg-type]
        media_host="127.0.0.1",
        media_port=18090,
        destinations=destinations,
        voice_factory=lambda *_: None,
        manager=CallManager(max_active_calls=0),
    )
    ari.on_event(service._handle_event)

    await ari.deliver(caller_start("caller-2"))

    assert ari.answered == [], "a call was answered while at capacity"
    assert "caller-2" in ari.hangups


# --- Media address: bind vs advertised --------------------------------------
def _telephony_settings(**overrides):
    """Settings with telephony fully configured, ignoring any local .env.

    `_env_file=None` matters: without it the developer's own .env leaks in and
    silently supplies the very field under test.
    """
    from app.core.config import Settings

    base = {
        "telephony_enabled": True,
        "stt_provider": "faster_whisper",
        "tts_provider": "piper",
        "vad_provider": "silero",
        "asterisk_ari_user": "clinic-ai",
        "asterisk_ari_password": "test-only-not-a-real-secret",
        "telephony_media_advertise_host": "",
    }
    return Settings(_env_file=None, **{**base, **overrides})


def test_a_wildcard_bind_without_an_advertised_host_is_refused() -> None:
    """Asterisk cannot dial 0.0.0.0.

    Binding wide is right when the PBX is on another host, but then something
    has to tell Asterisk *which* address to come back to. Left unset, the call
    connects, the media channel never does, and the caller hears silence — so
    this fails at start-up, where the message can name the setting to fix.
    """
    from app.core.errors import ProviderUnavailableError
    from app.telephony.factory import build_telephony_service

    settings = _telephony_settings(telephony_media_host="0.0.0.0")  # noqa: S104

    with pytest.raises(ProviderUnavailableError, match="ADVERTISE"):
        build_telephony_service(settings=settings, voice_factory=lambda *_: None)


def test_a_loopback_bind_advertises_itself() -> None:
    """PBX and application on one host: the two addresses are legitimately equal."""
    from app.telephony.factory import build_telephony_service

    settings = _telephony_settings(telephony_media_host="127.0.0.1")
    service = build_telephony_service(settings=settings, voice_factory=lambda *_: None)

    assert service._media_advertise_host == "127.0.0.1"


def test_the_advertised_host_is_what_asterisk_is_told_to_dial() -> None:
    """The bind address must never reach Asterisk as a media destination."""
    from app.telephony.factory import build_telephony_service

    settings = _telephony_settings(
        telephony_media_host="0.0.0.0",  # noqa: S104
        telephony_media_advertise_host="192.0.2.10",  # TEST-NET-1, per RFC 5737
    )
    service = build_telephony_service(settings=settings, voice_factory=lambda *_: None)

    assert service._media_advertise_host == "192.0.2.10"
    assert service._media_host == "0.0.0.0"  # noqa: S104


@pytest.mark.parametrize(
    "advertised",
    [
        "0.0.0.0 ",  # a trailing space is not a different address
        " ",  # truthy, so it does not fall back to the bind host
        "[::]",  # the same wildcard, spelled for a URL
        "0:0:0:0:0:0:0:0",
        "0",  # classic shorthand for the unspecified address
    ],
)
def test_wildcards_are_refused_however_they_are_spelled(advertised: str) -> None:
    """The guard is what stands between a typo and a silent, silent call.

    Each of these reaches Asterisk as `AudioSocket/<it>:8090/<uuid>` and is not
    dialable. A whitespace value is the nastiest: it is truthy, so it does not
    fall back to the bind host, and it is not the empty string, so a naive
    equality check waves it through.
    """
    from app.core.errors import ProviderUnavailableError
    from app.telephony.factory import build_telephony_service

    settings = _telephony_settings(
        telephony_media_host="0.0.0.0",  # noqa: S104
        telephony_media_advertise_host=advertised,
    )

    with pytest.raises(ProviderUnavailableError, match="ADVERTISE"):
        build_telephony_service(settings=settings, voice_factory=lambda *_: None)


# --- Health address: what the dialplan is told to curl ----------------------
def test_the_health_url_is_advertised_on_the_pbx_facing_address() -> None:
    """The dialplan curls this from inside the VM, so loopback is the VM.

    Asterisk reaches this process on the same network it reaches the media
    listener on, so the advertised media host answers this too — rather than a
    second setting that could drift out of step with the first.
    """
    settings = _telephony_settings(
        telephony_media_host="0.0.0.0",  # noqa: S104
        telephony_media_advertise_host="192.0.2.10",  # TEST-NET-1, per RFC 5737
        app_port=8000,
    )

    assert settings.health_advertise_url == "http://192.0.2.10:8000/health/live"


def test_the_health_url_follows_the_bind_host_when_all_on_one_host() -> None:
    """One host: the advertised address is legitimately loopback."""
    settings = _telephony_settings(telephony_media_host="127.0.0.1", app_port=9000)

    assert settings.health_advertise_url == "http://127.0.0.1:9000/health/live"


# --- Credentials that were never actually set -------------------------------
@pytest.mark.parametrize("placeholder", ["CHANGE_ME", "change-me", "changeme", " CHANGE_ME "])
def test_an_unsubstituted_ari_password_is_not_a_configuration(placeholder: str) -> None:
    """A placeholder cannot authenticate, so telephony must decline to start.

    Starting on one buys a permanent 401 reconnect loop and a /health that
    reports `down` — which reads as "the PBX is broken" when the truth is
    "this was never filled in". Declining routes callers to reception and says
    so, which is the failure the dialplan fallback exists to handle.
    """
    settings = _telephony_settings(asterisk_ari_password=placeholder)

    assert settings.telephony_configured is False


def test_a_real_ari_password_configures_telephony() -> None:
    """The guard must not reject an ordinary credential."""
    settings = _telephony_settings(asterisk_ari_password="an-actual-generated-secret")

    assert settings.telephony_configured is True
