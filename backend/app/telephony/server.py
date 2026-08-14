"""AudioSocket media server.

Asterisk connects *to us* when a call's dialplan reaches ``AudioSocket()``. One
TCP connection is one call's media, for the life of that call.

The server does exactly three things: accept, correlate, hand off. It holds no
clinic state and makes no call-control decisions — those belong to
``CallManager`` and ``CallController`` respectively.

Correlation matters. ARI tells us a call has arrived and gives us a channel to
control; this socket carries that call's audio. The link between them is the
UUID Asterisk sends as the first frame, which the dialplan set. Without it a
second concurrent call could be attached to the first call's conversation —
the crossover failure in the threat model.

**Bind to loopback.** The default host is 127.0.0.1: this port is unauthenticated
by design (Asterisk offers no AudioSocket credential), so anything that can
reach it can inject audio into a call. Where Asterisk runs on another host, the
link belongs on a private media network, not the internet.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable

from app.core.logging import get_logger
from app.core.metrics import M, metrics
from app.telephony.transport import AudioSocketChannel, TelephonyFormat

logger = get_logger(__name__)

#: How long to wait for the opening UUID frame before treating the connection
#: as unattributable. Generous — it is the first thing Asterisk writes.
UUID_TIMEOUT_SECONDS = 5.0

#: Handler signature: given a media channel and the UUID Asterisk supplied,
#: take ownership of the call. Returning ends the call's media.
MediaHandler = Callable[[AudioSocketChannel, str | None], Awaitable[None]]


class AudioSocketServer:
    """Accepts AudioSocket media connections from Asterisk."""

    def __init__(
        self,
        handler: MediaHandler,
        *,
        host: str = "127.0.0.1",
        port: int = 8090,
        wire_format: TelephonyFormat = TelephonyFormat.SLIN,
        resample_quality: str = "HQ",
        max_connections: int = 8,
    ) -> None:
        self._handler = handler
        self._host = host
        self._port = port
        self._format = wire_format
        self._resample_quality = resample_quality
        self._max_connections = max_connections

        self._server: asyncio.Server | None = None
        self._connections: set[asyncio.Task[None]] = set()
        self._active = 0

    @property
    def is_listening(self) -> bool:
        return self._server is not None and self._server.is_serving()

    @property
    def active_connections(self) -> int:
        return self._active

    @property
    def port(self) -> int:
        """Bound port. Differs from the configured one only when it was 0."""
        if self._server is not None and self._server.sockets:
            return int(self._server.sockets[0].getsockname()[1])
        return self._port

    async def start(self) -> None:
        if self._server is not None:
            return
        self._server = await asyncio.start_server(self._accept, self._host, self._port)
        logger.info(
            "telephony.media_listening",
            host=self._host,
            port=self.port,
            wire_format=self._format.value,
        )

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            with contextlib.suppress(Exception):
                await self._server.wait_closed()
            self._server = None
        for task in list(self._connections):
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._connections.clear()
        self._active = 0

    async def _accept(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if self._active >= self._max_connections:
            # Refuse rather than accept and thrash. Capacity policy proper
            # lives in CallManager; this is the media-layer backstop.
            logger.warning("telephony.media_rejected", reason="max_connections")
            metrics.increment(M.TELEPHONY_CALLS_REJECTED, labels={"reason": "media_capacity"})
            with contextlib.suppress(Exception):
                writer.close()
                await writer.wait_closed()
            return

        # Construct BEFORE reserving the slot. The counter was incremented
        # first and the constructor sat outside the try below, so anything it
        # raised leaked a media slot permanently — and after max_connections
        # such failures every subsequent call is refused until the process
        # restarts, with the port still open and still accepting.
        try:
            channel = AudioSocketChannel(
                reader,
                writer,
                call_id="pending",
                wire_format=self._format,
                resample_quality=self._resample_quality,
            )
        except Exception as exc:
            # Nothing has been reserved yet, so there is nothing to release —
            # but the socket is open and the exception would otherwise escape
            # into the accept task, where asyncio only reports it as an
            # unretrieved exception long afterwards.
            metrics.increment(M.TELEPHONY_MEDIA_ERROR, labels={"kind": "channel_setup"})
            logger.error("telephony.media_channel_setup_failed", error=type(exc).__name__)
            with contextlib.suppress(Exception):
                writer.close()
                await writer.wait_closed()
            return

        task = asyncio.current_task()
        if task is not None:
            self._connections.add(task)
        self._active += 1
        try:
            await channel.start()
            call_uuid = await channel.wait_for_uuid(limit_seconds=UUID_TIMEOUT_SECONDS)
            if call_uuid is None:
                logger.warning("telephony.media_unattributed")
                metrics.increment(M.TELEPHONY_MEDIA_ERROR, labels={"kind": "no_uuid"})
                return
            await self._handler(channel, call_uuid)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            metrics.increment(M.TELEPHONY_MEDIA_ERROR, labels={"kind": "handler"})
            logger.error("telephony.media_handler_failed", error=type(exc).__name__)
        finally:
            self._active -= 1
            if task is not None:
                self._connections.discard(task)
            await channel.close(notify_peer=False)
