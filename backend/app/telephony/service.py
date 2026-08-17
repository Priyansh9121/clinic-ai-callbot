"""Ties the control plane, the media plane and the voice pipeline together.

The call setup sequence, which is the part worth understanding:

    1.  Asterisk routes the AI extension to ``Stasis(clinic-ai)``.
    2.  ARI delivers ``StasisStart`` for the caller's channel. We mint an opaque
        ``call_id`` and a media UUID, and answer.
    3.  We create a mixing bridge and originate a channel to the AudioSocket
        channel driver, carrying that media UUID.
    4.  Asterisk connects to our media server. The first frame is the UUID,
        which is how this socket is matched to that call rather than to some
        other call in flight.
    5.  Both channels join the bridge. Audio now flows caller ↔ us, while the
        caller's channel stays under ARI control — which is what makes a later
        transfer possible.
    6.  ``CallSession`` runs the existing ``VoiceSession`` over that media.

Sending the caller back to the dialplan to run ``AudioSocket()`` would be one
step shorter and is a dead end: the channel leaves Stasis, and with it goes any
ability to transfer the caller to a human.

**Untested against a live PBX.** Steps 1 to 5 are written to the documented ARI
and AudioSocket contracts but have not been run against Asterisk in this
environment — see docs/development/local-sip.md for what the user must verify.
Everything from step 6 onward is exercised by the test suite over real sockets.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import Callable
from typing import Any

from app.core.logging import get_logger
from app.core.metrics import M, metrics
from app.telephony.ari import AriClient, AriError
from app.telephony.asterisk_control import AsteriskCallController
from app.telephony.control import HangupCause
from app.telephony.destinations import DestinationRegistry
from app.telephony.manager import (
    DEFAULT_MAX_CALL_SECONDS,
    CallManager,
    CallSession,
    CapacityPolicy,
    build_caller,
    build_metadata,
    new_call_id,
)
from app.telephony.server import AudioSocketServer
from app.telephony.transport import AudioSocketChannel, TelephonyFormat

logger = get_logger(__name__)

#: How long a call waits for its media socket before being given up on.
MEDIA_ATTACH_TIMEOUT_SECONDS = 10.0

#: Factory supplied by the application: given the two audio transports and an
#: event callback, produce a running ``VoiceSession`` with its own engine.
VoiceFactory = Callable[..., Any]


class _PendingCall:
    """A call that has been answered and is waiting for its media socket."""

    __slots__ = ("bridge_id", "call_id", "controller", "finished", "media", "media_uuid")

    def __init__(self, *, call_id: str, media_uuid: str, bridge_id: str, controller: Any) -> None:
        self.call_id = call_id
        self.media_uuid = media_uuid
        self.bridge_id = bridge_id
        self.controller = controller
        self.media: asyncio.Future[AudioSocketChannel] = asyncio.get_running_loop().create_future()
        #: Set when the call is over, so the media server can release the
        #: socket. Without it the media handler would have to poll the call
        #: registry and race its registration.
        self.finished = asyncio.Event()


class TelephonyService:
    """Owns the PBX connection, the media server and the live-call registry."""

    def __init__(
        self,
        *,
        ari: AriClient,
        media_host: str,
        media_port: int,
        destinations: DestinationRegistry,
        voice_factory: VoiceFactory,
        manager: CallManager | None = None,
        server: AudioSocketServer | None = None,
        hangup_drain_ms: int = 250,
        max_call_seconds: int = DEFAULT_MAX_CALL_SECONDS,
        media_advertise_host: str | None = None,
        wire_format: TelephonyFormat = TelephonyFormat.SLIN,
        resample_quality: str = "HQ",
    ) -> None:
        self._ari = ari
        self._media_host = media_host
        # What Asterisk is told to dial. Distinct from the bind address: with
        # the PBX in a VM we bind 0.0.0.0 but must advertise the host's address
        # on that network. Defaults to the bind host for an all-on-one-host
        # setup, which is the only case where they are legitimately the same.
        self._media_advertise_host = media_advertise_host or media_host
        self._media_port = media_port
        self._destinations = destinations
        self._voice_factory = voice_factory
        self._manager = manager or CallManager()
        self._hangup_drain_ms = hangup_drain_ms
        self._max_call_seconds = max_call_seconds
        self._format = wire_format

        self._server = server or AudioSocketServer(
            self._attach_media,
            host=media_host,
            port=media_port,
            wire_format=wire_format,
            resample_quality=resample_quality,
        )
        # Keyed by media UUID: the only thing linking a socket to a call.
        self._pending: dict[str, _PendingCall] = {}
        self._controllers: dict[str, AsteriskCallController] = {}
        self._tasks: set[asyncio.Task[None]] = set()

    @property
    def manager(self) -> CallManager:
        return self._manager

    @property
    def media_port(self) -> int:
        return self._server.port

    @property
    def is_listening(self) -> bool:
        return self._server.is_listening

    # --- Lifecycle ---------------------------------------------------------
    async def start(self) -> None:
        await self._server.start()
        self._ari.on_event(self._handle_event)
        await self._ari.start()

    async def stop(self) -> None:
        for task in list(self._tasks):
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._tasks.clear()
        await self._manager.shutdown()
        await self._server.stop()
        await self._ari.close()

    # --- ARI events --------------------------------------------------------
    async def _handle_event(self, event: dict[str, Any]) -> None:
        kind = event.get("type")
        if kind == "StasisStart":
            await self._on_stasis_start(event)
        elif kind in ("StasisEnd", "ChannelHangupRequest", "ChannelDestroyed"):
            self._on_channel_gone(event)
        elif kind == "ChannelDtmfReceived":
            self._on_dtmf(event)

    async def _on_stasis_start(self, event: dict[str, Any]) -> None:
        channel = event.get("channel") or {}
        channel_id = str(channel.get("id", ""))
        args = event.get("args") or []

        # The media channel we originated re-enters Stasis; it is not a caller.
        # Its media UUID travels in the Stasis arguments, so this works whether
        # the event arrives before or after the originate response.
        if args and args[0] == "media":
            media_uuid = str(args[1]) if len(args) > 1 else ""
            await self._join_media_channel(channel_id, media_uuid)
            return
        if not channel_id:
            return

        if self._manager.at_capacity:
            metrics.increment(M.TELEPHONY_CALLS_REJECTED, labels={"reason": "capacity"})
            if self._manager.capacity_policy is CapacityPolicy.TRANSFER_RECEPTION:
                target = self._destinations.for_reason(None)
                with contextlib.suppress(AriError):
                    await self._ari.continue_in_dialplan(
                        channel_id, context=target.context, extension=target.extension
                    )
            else:
                # Leave the call unanswered: the dialplan's existing failover
                # sends it to reception, which is the Milestone 1 arrangement.
                with contextlib.suppress(AriError):
                    await self._ari.hangup(channel_id)
            return

        await self._begin_call(channel_id, channel)

    async def _begin_call(self, channel_id: str, channel: dict[str, Any]) -> None:
        call_id = new_call_id()
        media_uuid = str(uuid.uuid4())
        bridge_id = f"clinic-{call_id}"
        caller_info = channel.get("caller") or {}

        controller = AsteriskCallController(
            client=self._ari,
            channel_id=channel_id,
            call_id=call_id,
            caller=build_caller(caller_info.get("number"), caller_info.get("name")),
            metadata=build_metadata(
                call_id,
                extension=str((channel.get("dialplan") or {}).get("exten") or "") or None,
                channel=channel_id,
            ),
        )
        self._controllers[channel_id] = controller
        pending = _PendingCall(
            call_id=call_id, media_uuid=media_uuid, bridge_id=bridge_id, controller=controller
        )
        self._pending[media_uuid] = pending

        logger.info("telephony.call_started", call_id=call_id, **controller.caller.log_fields())
        try:
            await controller.answer()
            await self._ari.create_bridge(bridge_id)
            await self._ari.add_to_bridge(bridge_id, channel_id)
            # The UUID goes in appArgs, not just the endpoint string, so the
            # channel's own StasisStart identifies which call it belongs to.
            # Correlating on the id returned here instead would be a race: the
            # WebSocket event and this HTTP response are independent, and
            # Asterisk can deliver the event first — leaving the media channel
            # out of the bridge and the caller listening to silence.
            await self._ari.originate_media_channel(
                host=self._media_advertise_host,
                port=self.media_port,
                media_uuid=media_uuid,
                app_args=f"media,{media_uuid}",
                # Name the format. Asterisk's own choice is the best the
                # channel can do, not the one this process decodes.
                media_format=self._format.asterisk_name,
            )
        except AriError as exc:
            metrics.increment(M.TELEPHONY_CALLS_FAILED, labels={"stage": "setup"})
            logger.error("telephony.setup_failed", call_id=call_id, error=str(exc))
            self._pending.pop(media_uuid, None)
            self._controllers.pop(channel_id, None)
            with contextlib.suppress(AriError):
                await self._ari.hangup(channel_id)
            # The bridge may already exist: create_bridge is the second step,
            # so a failure in add_to_bridge or originate leaves one behind.
            # Only the success path destroyed it, so every failed setup
            # orphaned a bridge in Asterisk until the PBX restarted.
            await self._destroy_bridge(bridge_id)
            return

        self._spawn(self._await_media(pending))

    async def _join_media_channel(self, channel_id: str, media_uuid: str) -> None:
        """Put the originated media channel into its call's bridge."""
        pending = self._pending.get(media_uuid)
        if pending is None:
            # A stale or duplicated event for a call that has already finished.
            logger.debug("telephony.media_channel_unmatched")
            return
        with contextlib.suppress(AriError):
            await self._ari.add_to_bridge(pending.bridge_id, channel_id)

    async def _await_media(self, pending: _PendingCall) -> None:
        """Wait for the media socket, then run the call."""
        try:
            async with asyncio.timeout(MEDIA_ATTACH_TIMEOUT_SECONDS):
                channel = await pending.media
        except TimeoutError:
            metrics.increment(M.TELEPHONY_CALLS_FAILED, labels={"stage": "media_attach"})
            logger.error("telephony.media_never_arrived", call_id=pending.call_id)
            self._pending.pop(pending.media_uuid, None)
            pending.finished.set()
            with contextlib.suppress(Exception):
                await pending.controller.hangup(HangupCause.FAILURE)
            await self._destroy_bridge(pending.bridge_id)
            return

        session = CallSession(
            call_id=pending.call_id,
            controller=pending.controller,
            channel=channel,
            voice_session_factory=self._voice_factory,
            destinations=self._destinations,
            hangup_drain_ms=self._hangup_drain_ms,
            max_call_seconds=self._max_call_seconds,
        )
        if not await self._manager.register(session):
            pending.finished.set()
            with contextlib.suppress(Exception):
                await pending.controller.hangup(HangupCause.FAILURE)
            self._pending.pop(pending.media_uuid, None)
            await self._destroy_bridge(pending.bridge_id)
            return
        try:
            await session.run()
        finally:
            pending.finished.set()
            # Hand back whatever the voice factory allocated for this call —
            # its database session, in the application wiring. Optional so a
            # plain callable factory (the tests use one) still works.
            release = getattr(self._voice_factory, "release", None)
            if release is not None:
                with contextlib.suppress(Exception):
                    await release(session.voice)
            await self._manager.release(pending.call_id)
            self._pending.pop(pending.media_uuid, None)
            await self._destroy_bridge(pending.bridge_id)

    async def _destroy_bridge(self, bridge_id: str) -> None:
        """Release a bridge on every path that created one.

        Only the success path used to do this, so a setup failure, a media
        socket that never arrived, or a call refused at capacity each left a
        bridge in Asterisk until the PBX was restarted. Destroying one that was
        never created is harmless and suppressed.
        """
        with contextlib.suppress(AriError):
            await self._ari.destroy_bridge(bridge_id)

    async def _attach_media(self, channel: AudioSocketChannel, media_uuid: str | None) -> None:
        """Media server callback: match this socket to the call that owns it."""
        pending = self._pending.get(media_uuid or "")
        if pending is None:
            logger.warning("telephony.media_unknown_call")
            metrics.increment(M.TELEPHONY_MEDIA_ERROR, labels={"kind": "unmatched"})
            await channel.close(notify_peer=True)
            return
        if not pending.media.done():
            pending.media.set_result(channel)
        # Hold the connection open until the call is over — the media server
        # closes the socket when this returns.
        await pending.finished.wait()

    def _on_channel_gone(self, event: dict[str, Any]) -> None:
        channel_id = str((event.get("channel") or {}).get("id", ""))
        controller = self._controllers.pop(channel_id, None)
        if controller is not None:
            controller.notify_hangup(HangupCause.CALLER)

    def _on_dtmf(self, event: dict[str, Any]) -> None:
        channel_id = str((event.get("channel") or {}).get("id", ""))
        digit = str(event.get("digit", ""))
        controller = self._controllers.get(channel_id)
        if controller is not None and digit:
            controller.notify_dtmf(digit)

    def _spawn(self, coro: Any) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
