"""``CallController`` backed by Asterisk ARI.

One instance per call. Owns nothing but the channel id and the callbacks; every
network operation is delegated to the shared ``AriClient``.

Transfer is the security-critical operation here, so it is worth being explicit
about why it is safe. The method takes a ``TransferTarget`` — a value that can
only be produced by ``DestinationRegistry.resolve()`` from a
``TransferDestination`` enum member — and hands its context and extension to
``continueInDialplan``. There is no string path from a transcript to this call.
Even if there were, the dialplan context is a transfer-only context containing
the approved extensions and no outbound trunk, so Asterisk itself would refuse.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable

from app.core.logging import get_logger
from app.core.metrics import M, metrics
from app.telephony.ari import AriClient, AriError
from app.telephony.control import (
    DtmfEvent,
    HangupCause,
    TransferOutcome,
    TransferResult,
)
from app.telephony.destinations import TransferTarget
from app.telephony.metadata import CallerIdentity, CallMetadata

logger = get_logger(__name__)


class AsteriskCallController:
    """Signalling for one Asterisk channel."""

    def __init__(
        self,
        *,
        client: AriClient,
        channel_id: str,
        call_id: str,
        caller: CallerIdentity,
        metadata: CallMetadata,
    ) -> None:
        self._client = client
        self._channel_id = channel_id
        self._call_id = call_id
        self._caller = caller
        self._metadata = metadata

        self._answered = False
        self._released = False
        self._on_hangup: Callable[[HangupCause], None] | None = None
        self._on_dtmf: Callable[[DtmfEvent], None] | None = None

    # --- Introspection -----------------------------------------------------
    @property
    def call_id(self) -> str:
        return self._call_id

    @property
    def channel_id(self) -> str:
        return self._channel_id

    @property
    def caller(self) -> CallerIdentity:
        return self._caller

    @property
    def metadata(self) -> CallMetadata:
        return self._metadata

    @property
    def is_answered(self) -> bool:
        return self._answered

    @property
    def is_released(self) -> bool:
        return self._released

    # --- Operations --------------------------------------------------------
    async def answer(self) -> None:
        if self._answered or self._released:
            return
        await self._client.answer(self._channel_id)
        self._answered = True
        metrics.increment(M.TELEPHONY_CALLS_ANSWERED)
        logger.info("telephony.answered", call_id=self._call_id)

    async def hangup(self, cause: HangupCause = HangupCause.APPLICATION) -> None:
        """Release the channel. Safe to call after the caller has already gone."""
        if self._released:
            return
        self._released = True
        try:
            await self._client.hangup(self._channel_id)
        except AriError as exc:
            # The usual reason is that the caller hung up first and the channel
            # is already gone. That is the normal end of a call, not a fault.
            logger.debug("telephony.hangup_noop", call_id=self._call_id, error=str(exc))
        logger.info("telephony.hungup", call_id=self._call_id, cause=cause.value)

    async def transfer(self, target: TransferTarget) -> TransferResult:
        """Send the caller to an approved destination."""
        if self._released:
            return TransferResult(TransferOutcome.FAILED, target, detail="channel_released")
        try:
            await self._client.continue_in_dialplan(
                self._channel_id,
                context=target.context,
                extension=target.extension,
            )
        except AriError as exc:
            metrics.increment(
                M.TELEPHONY_TRANSFER_FAILED, labels={"destination": target.destination.value}
            )
            logger.warning(
                "telephony.transfer_failed",
                call_id=self._call_id,
                destination=target.destination.value,
                error=str(exc),
            )
            return TransferResult(TransferOutcome.UNAVAILABLE, target, detail="ari_error")

        # The AI leg ends here: the channel is now executing dialplan we do not
        # control. Whether a human actually picks up is decided beyond this
        # point, and the dialplan's own failover (voicemail) handles it.
        self._released = True
        metrics.increment(
            M.TELEPHONY_CALLS_TRANSFERRED, labels={"destination": target.destination.value}
        )
        logger.info(
            "telephony.transferred",
            call_id=self._call_id,
            destination=target.destination.value,
            extension=target.extension,
        )
        return TransferResult(TransferOutcome.COMPLETED, target)

    # --- Events ------------------------------------------------------------
    def on_hangup(self, callback: Callable[[HangupCause], None]) -> None:
        self._on_hangup = callback

    def on_dtmf(self, callback: Callable[[DtmfEvent], None]) -> None:
        self._on_dtmf = callback

    def notify_hangup(self, cause: HangupCause = HangupCause.CALLER) -> None:
        """Called by the event router when Asterisk reports the channel gone."""
        self._released = True
        if self._on_hangup is not None:
            callback, self._on_hangup = self._on_hangup, None
            with contextlib.suppress(Exception):
                callback(cause)

    def notify_dtmf(self, digit: str) -> None:
        if self._on_dtmf is not None:
            with contextlib.suppress(Exception):
                self._on_dtmf(DtmfEvent(digit=digit, call_id=self._call_id))


class NullCallController:
    """A controller for media-only deployments, and for tests.

    Used when AudioSocket is driven straight from the dialplan with no ARI
    control channel: audio works, and hanging up means closing the socket.
    Transfer is genuinely unavailable, and says so rather than pretending.
    """

    def __init__(
        self,
        *,
        call_id: str,
        caller: CallerIdentity | None = None,
        metadata: CallMetadata | None = None,
    ) -> None:
        self._call_id = call_id
        self._caller = caller or CallerIdentity()
        self._metadata = metadata or CallMetadata(call_id=call_id)
        self._released = False
        self._hangup_event = asyncio.Event()
        self._on_hangup: Callable[[HangupCause], None] | None = None
        self._on_dtmf: Callable[[DtmfEvent], None] | None = None

    @property
    def call_id(self) -> str:
        return self._call_id

    @property
    def caller(self) -> CallerIdentity:
        return self._caller

    @property
    def metadata(self) -> CallMetadata:
        return self._metadata

    @property
    def is_released(self) -> bool:
        return self._released

    async def answer(self) -> None:
        return None

    async def hangup(self, cause: HangupCause = HangupCause.APPLICATION) -> None:
        self._released = True
        self._hangup_event.set()

    async def transfer(self, target: TransferTarget) -> TransferResult:
        return TransferResult(TransferOutcome.FAILED, target, detail="no_call_control_configured")

    def on_hangup(self, callback: Callable[[HangupCause], None]) -> None:
        self._on_hangup = callback

    def on_dtmf(self, callback: Callable[[DtmfEvent], None]) -> None:
        self._on_dtmf = callback

    def notify_hangup(self, cause: HangupCause = HangupCause.CALLER) -> None:
        self._released = True
        self._hangup_event.set()
        if self._on_hangup is not None:
            callback, self._on_hangup = self._on_hangup, None
            with contextlib.suppress(Exception):
                callback(cause)

    def notify_dtmf(self, digit: str) -> None:
        if self._on_dtmf is not None:
            with contextlib.suppress(Exception):
                self._on_dtmf(DtmfEvent(digit=digit, call_id=self._call_id))
