"""Asterisk REST Interface — the call-control plane.

ARI is two connections with different jobs:

* a **WebSocket** that pushes channel events (a call arrived, a caller hung up,
  a digit was pressed);
* a **REST API** for operations (answer, hang up, return to the dialplan).

Only signalling passes through here. Audio never does — that is AudioSocket's
job — which is why a slow REST call cannot stutter a caller's speech.

Python is not a SIP stack and must not become one. There is no REGISTER, no
INVITE, no SDP and no digest authentication in this codebase: Asterisk owns
every bit of SIP, and this module speaks to Asterisk's own control API.

**Reconnection.** A PBX restart must not take the backend down, and must not
spin. The event loop reconnects with exponential backoff and jitter, and the
text API and local voice CLI keep working throughout — telephony reports
``down`` in ``/health`` rather than failing the process.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import random
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from app.core.logging import get_logger
from app.core.metrics import M, metrics

logger = get_logger(__name__)

#: Backoff bounds for reconnecting to the PBX.
_RECONNECT_MIN_SECONDS = 0.5
_RECONNECT_MAX_SECONDS = 30.0

#: REST calls are signalling: they should be quick or they should fail, because
#: a caller is waiting on the far end of every one of them.
DEFAULT_REQUEST_TIMEOUT = 5.0

AriEvent = dict[str, Any]
EventHandler = Callable[[AriEvent], Awaitable[None]]


class AriError(RuntimeError):
    """An ARI operation failed."""


class AriClient:
    """Connection to Asterisk's REST interface and event stream.

    One instance per process, shared by every call. Holds credentials, so it
    never logs its own URL with the query string attached — ARI takes the API
    key as a URL parameter, which is exactly the sort of thing that ends up in
    a log file.
    """

    def __init__(
        self,
        *,
        base_url: str,
        username: str,
        password: str,
        app_name: str = "clinic-ai",
        request_timeout: float = DEFAULT_REQUEST_TIMEOUT,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._username = username
        self._password = password
        self._app_name = app_name
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            auth=(username, password),
            timeout=request_timeout,
        )
        self._handler: EventHandler | None = None
        self._task: asyncio.Task[None] | None = None
        self._connected = asyncio.Event()
        self._closing = False
        self.reconnects = 0

    # --- Introspection -----------------------------------------------------
    @property
    def app_name(self) -> str:
        return self._app_name

    @property
    def is_connected(self) -> bool:
        return self._connected.is_set()

    @property
    def safe_url(self) -> str:
        """Base URL with no credentials, for logs and the status command."""
        return self._base_url

    # --- Lifecycle ---------------------------------------------------------
    def on_event(self, handler: EventHandler) -> None:
        self._handler = handler

    async def start(self) -> None:
        if self._task is None:
            self._closing = False
            self._task = asyncio.create_task(self._event_loop())

    async def close(self) -> None:
        self._closing = True
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
            self._task = None
        self._connected.clear()
        await self._client.aclose()

    async def wait_connected(self, *, limit_seconds: float) -> bool:
        with contextlib.suppress(TimeoutError):
            async with asyncio.timeout(limit_seconds):
                await self._connected.wait()
        return self._connected.is_set()

    async def _event_loop(self) -> None:
        """Consume the ARI event stream, reconnecting with backoff."""
        import websockets

        delay = _RECONNECT_MIN_SECONDS
        first_attempt = True
        while not self._closing:
            try:
                url = (
                    self._base_url.replace("https://", "wss://").replace("http://", "ws://")
                    + f"/events?app={self._app_name}&subscribeAll=false"
                )
                async with websockets.connect(
                    url,
                    additional_headers=self._auth_header(),
                    ping_interval=20,
                    ping_timeout=20,
                ) as socket:
                    if not first_attempt:
                        self.reconnects += 1
                        metrics.increment(M.TELEPHONY_PBX_RECONNECTS)
                    first_attempt = False
                    delay = _RECONNECT_MIN_SECONDS
                    self._connected.set()
                    logger.info("telephony.ari_connected", app=self._app_name)
                    async for raw in socket:
                        await self._dispatch(raw)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("telephony.ari_disconnected", error=type(exc).__name__)
            finally:
                self._connected.clear()

            if self._closing:
                return
            # Jitter so a PBX restart does not have every instance reconnect on
            # the same tick.
            sleep_for = min(delay, _RECONNECT_MAX_SECONDS) * (0.5 + random.random())  # noqa: S311
            await asyncio.sleep(sleep_for)
            delay = min(delay * 2, _RECONNECT_MAX_SECONDS)

    def _auth_header(self) -> dict[str, str]:
        import base64

        token = base64.b64encode(f"{self._username}:{self._password}".encode()).decode()
        return {"Authorization": f"Basic {token}"}

    async def _dispatch(self, raw: str | bytes) -> None:
        try:
            event = json.loads(raw)
        except (ValueError, TypeError):
            metrics.increment(M.TELEPHONY_MEDIA_ERROR, labels={"kind": "ari_decode"})
            return
        if self._handler is None:
            return
        try:
            await self._handler(event)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # One malformed or unexpected event must not end the event stream
            # and with it every future call.
            # NOT `event=`: that is structlog's own first parameter, and
            # passing it as a keyword raises TypeError — inside the handler
            # whose entire purpose is that one malformed event must not end
            # the stream.
            logger.error(
                "telephony.ari_handler_failed",
                event_type=str(event.get("type")),
                error=type(exc).__name__,
            )

    # --- Operations --------------------------------------------------------
    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        try:
            response = await self._client.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise AriError(f"{method} {path} failed: {type(exc).__name__}") from exc
        if response.status_code >= 400:
            raise AriError(f"{method} {path} returned {response.status_code}")
        return response

    async def answer(self, channel_id: str) -> None:
        await self._request("POST", f"/channels/{channel_id}/answer")

    async def hangup(self, channel_id: str) -> None:
        await self._request("DELETE", f"/channels/{channel_id}")

    async def continue_in_dialplan(
        self, channel_id: str, *, context: str, extension: str, priority: int = 1
    ) -> None:
        """Return the channel to a specific dialplan location.

        This is how a transfer is performed, and deliberately *not* an
        originate-and-bridge: the destination is a context/extension pair that
        must exist in the dialplan, so the set of reachable destinations is
        fixed by Asterisk configuration rather than by anything this process
        computes at runtime.
        """
        await self._request(
            "POST",
            f"/channels/{channel_id}/continue",
            params={"context": context, "extension": extension, "priority": priority},
        )

    async def create_bridge(self, bridge_id: str) -> None:
        """A mixing bridge joins the caller to the media channel.

        This is what lets ARI keep control of the caller's channel while
        AudioSocket carries the audio. The alternative — sending the channel
        back to the dialplan to run ``AudioSocket()`` — hands the channel away
        and with it the ability to transfer.
        """
        await self._request("POST", "/bridges", params={"type": "mixing", "bridgeId": bridge_id})

    async def destroy_bridge(self, bridge_id: str) -> None:
        with contextlib.suppress(AriError):
            await self._request("DELETE", f"/bridges/{bridge_id}")

    async def add_to_bridge(self, bridge_id: str, channel_id: str) -> None:
        await self._request(
            "POST", f"/bridges/{bridge_id}/addChannel", params={"channel": channel_id}
        )

    async def originate_media_channel(
        self, *, host: str, port: int, media_uuid: str, app_args: str = "media"
    ) -> str:
        """Dial the AudioSocket channel driver and return its channel id.

        The endpoint is built from *configuration* plus a UUID we generated —
        never from caller input. It addresses our own media server, so it is
        not a dialable telephone destination in any sense.
        """
        response = await self._request(
            "POST",
            "/channels",
            params={
                "endpoint": f"AudioSocket/{host}:{port}/{media_uuid}",
                "app": self._app_name,
                "appArgs": app_args,
            },
        )
        return str(response.json()["id"])

    async def get_json(self, path: str) -> Any:
        """GET an ARI resource. For diagnostics and the live test suite."""
        response = await self._request("GET", path)
        return response.json()

    async def channel(self, channel_id: str) -> dict[str, Any]:
        response = await self._request("GET", f"/channels/{channel_id}")
        return dict(response.json())

    async def asterisk_info(self) -> dict[str, Any]:
        """Version and system info, for the health check and status command."""
        response = await self._request("GET", "/asterisk/info")
        return dict(response.json())

    async def ping(self) -> bool:
        """Cheap reachability probe that does not require the event socket."""
        try:
            await self.asterisk_info()
        except AriError:
            return False
        return True
