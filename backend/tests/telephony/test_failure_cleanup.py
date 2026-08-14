"""Failure injected at each setup stage must leave nothing behind.

Every stage between StasisStart and a running VoiceSession allocates something
in Asterisk or in this process. The dangerous failures are the ones between
allocations, because those are the paths nobody exercises by hand — and three
of them leaked a bridge until the PBX was restarted.
"""

from __future__ import annotations

import asyncio
import contextlib

import pytest

from app.telephony.ari import AriError
from app.telephony.destinations import DestinationRegistry, TransferDestination
from app.telephony.manager import CallManager
from app.telephony.server import AudioSocketServer
from app.telephony.service import TelephonyService
from tests.telephony.test_ari_call_setup import FakeAriClient, caller_start

pytestmark = pytest.mark.integration


class TrackingAri(FakeAriClient):
    """Counts bridges, and can be told to fail one specific operation."""

    def __init__(self, fail_on: str | None = None) -> None:
        super().__init__()
        self.fail_on = fail_on
        self.live_bridges: set[str] = set()

    async def create_bridge(self, bridge_id: str) -> None:
        if self.fail_on == "create_bridge":
            raise AriError("POST /bridges failed")
        self.live_bridges.add(bridge_id)
        await super().create_bridge(bridge_id)

    async def destroy_bridge(self, bridge_id: str) -> None:
        self.live_bridges.discard(bridge_id)

    async def add_to_bridge(self, bridge_id: str, channel_id: str) -> None:
        if self.fail_on == "add_to_bridge":
            raise AriError("POST /bridges/addChannel failed")
        await super().add_to_bridge(bridge_id, channel_id)

    async def originate_media_channel(self, **kwargs) -> str:
        if self.fail_on == "originate":
            raise AriError("POST /channels failed")
        return await super().originate_media_channel(**kwargs)


@pytest.fixture
def destinations() -> DestinationRegistry:
    return DestinationRegistry(
        {TransferDestination.RECEPTION: "100", TransferDestination.VOICEMAIL: "101"},
        context="clinic-transfer",
    )


def _service(ari: TrackingAri, destinations: DestinationRegistry) -> TelephonyService:
    service = TelephonyService(
        ari=ari,  # type: ignore[arg-type]
        media_host="127.0.0.1",
        media_port=18099,
        destinations=destinations,
        voice_factory=lambda *_: None,  # type: ignore[arg-type, return-value]
        manager=CallManager(max_active_calls=2),
    )
    ari.on_event(service._handle_event)
    return service


@pytest.mark.parametrize("stage", ["create_bridge", "add_to_bridge", "originate"])
async def test_a_setup_failure_leaves_no_bridge_and_no_pending_call(
    destinations, stage: str
) -> None:
    """Only the success path used to destroy the bridge."""
    ari = TrackingAri(fail_on=stage)
    service = _service(ari, destinations)

    await ari.deliver(caller_start())

    assert ari.live_bridges == set(), f"failing at {stage} orphaned a bridge in Asterisk"
    assert service._pending == {}, f"failing at {stage} left a pending call behind"
    assert ari.hangups, "the caller's channel was never released"


async def test_media_that_never_arrives_releases_the_bridge(destinations, monkeypatch) -> None:
    """Asterisk answers but never dials back: the commonest live misconfiguration."""
    from app.telephony import service as service_module

    monkeypatch.setattr(service_module, "MEDIA_ATTACH_TIMEOUT_SECONDS", 0.05)
    ari = TrackingAri()
    service = _service(ari, destinations)

    await ari.deliver(caller_start())
    assert ari.live_bridges, "no bridge was created, so this proves nothing"

    await asyncio.sleep(0.3)  # let the media wait time out

    assert ari.live_bridges == set(), "a bridge outlived a call whose media never arrived"
    assert service._pending == {}
    assert ari.hangups


async def test_a_media_slot_is_not_leaked_when_the_channel_cannot_be_built(monkeypatch) -> None:
    """The counter was incremented before the constructor ran, outside the try.

    After max_connections such failures every later call is refused, with the
    port still open and still accepting connections.
    """
    from app.telephony import server as server_module

    async def handler(_channel, _uuid):  # pragma: no cover - never reached
        return None

    media = AudioSocketServer(handler, host="127.0.0.1", port=0, max_connections=2)

    def explode(*_args, **_kwargs):
        raise RuntimeError("cannot build channel")

    monkeypatch.setattr(server_module, "AudioSocketChannel", explode)

    await media.start()
    try:
        port = media._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
        for _ in range(5):  # more than max_connections
            _reader, writer = await asyncio.open_connection("127.0.0.1", port)
            await asyncio.sleep(0.05)  # let the server accept and fail
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
    finally:
        await media.stop()

    assert media._active == 0, f"{media._active} media slots leaked and never come back"
