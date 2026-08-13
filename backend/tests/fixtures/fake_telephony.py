"""Test doubles for telephony. No Asterisk, no SIP, no network beyond loopback.

Two kinds of double live here, and the distinction matters:

* ``FakeCallController`` and ``FakePbx`` replace *signalling*. There is no way
  to test "the caller hung up during the LLM call" against a real PBX
  deterministically, so control is faked and asserted on.
* ``FakeAsterisk`` does **not** replace the media path. It speaks the real
  AudioSocket protocol over a real TCP socket, so framing, codec conversion,
  pacing and disconnect handling are exercised for real. Faking the media layer
  would leave the most failure-prone code in the milestone untested.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import Callable

from app.telephony.audiosocket import (
    Frame,
    FrameDecoder,
    FrameType,
    audio_frame,
    uuid_frame,
)
from app.telephony.control import DtmfEvent, HangupCause, TransferOutcome, TransferResult
from app.telephony.destinations import TransferTarget
from app.telephony.metadata import CallerIdentity, CallMetadata
from app.telephony.transport import AudioSocketChannel, TelephonyFormat


class FakeCallController:
    """Records signalling instead of performing it."""

    def __init__(
        self,
        *,
        call_id: str = "test-call",
        caller: CallerIdentity | None = None,
        transfer_outcome: TransferOutcome = TransferOutcome.COMPLETED,
        answer_error: Exception | None = None,
    ) -> None:
        self._call_id = call_id
        self._caller = caller or CallerIdentity.from_channel(number="+61491570156")
        self._metadata = CallMetadata(call_id=call_id, dialed_extension="500")
        self._transfer_outcome = transfer_outcome
        self._answer_error = answer_error

        self.answered = 0
        self.hangups: list[HangupCause] = []
        self.transfers: list[TransferTarget] = []
        self.on_hangup_callback: Callable[[HangupCause], None] | None = None
        self.on_dtmf_callback: Callable[[DtmfEvent], None] | None = None

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
        return bool(self.hangups)

    async def answer(self) -> None:
        if self._answer_error is not None:
            raise self._answer_error
        self.answered += 1

    async def hangup(self, cause: HangupCause = HangupCause.APPLICATION) -> None:
        self.hangups.append(cause)

    async def transfer(self, target: TransferTarget) -> TransferResult:
        self.transfers.append(target)
        return TransferResult(self._transfer_outcome, target)

    def on_hangup(self, callback: Callable[[HangupCause], None]) -> None:
        self.on_hangup_callback = callback

    def on_dtmf(self, callback: Callable[[DtmfEvent], None]) -> None:
        self.on_dtmf_callback = callback

    # --- Test controls -----------------------------------------------------
    def caller_hangs_up(self) -> None:
        if self.on_hangup_callback is not None:
            self.on_hangup_callback(HangupCause.CALLER)

    def press(self, digit: str) -> None:
        if self.on_dtmf_callback is not None:
            self.on_dtmf_callback(DtmfEvent(digit=digit, call_id=self._call_id))


class FakeAsterisk:
    """The far end of a real AudioSocket connection.

    Runs a loopback TCP server, speaks the genuine wire protocol, and records
    everything the agent transmits so tests can assert on real bytes.
    """

    def __init__(self, *, wire_format: TelephonyFormat = TelephonyFormat.SLIN) -> None:
        self._format = wire_format
        self._server: asyncio.Server | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._connected = asyncio.Event()
        self.received = bytearray()
        self.frame_sizes: list[int] = []
        self.terminated = False
        self.call_uuid = uuid.uuid4()
        self._line: asyncio.Task[None] | None = None

    @property
    def port(self) -> int:
        assert self._server is not None and self._server.sockets
        return int(self._server.sockets[0].getsockname()[1])

    @property
    def received_ms(self) -> float:
        width = 1 if self._format in (TelephonyFormat.PCMU, TelephonyFormat.PCMA) else 2
        return len(self.received) / (self._format.sample_rate * width) * 1000

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._writer = writer
        writer.write(uuid_frame(self.call_uuid))
        await writer.drain()
        self._connected.set()
        decoder = FrameDecoder()
        try:
            while True:
                data = await reader.read(4096)
                if not data:
                    return
                for frame in decoder.feed(data):
                    if frame.type is FrameType.AUDIO:
                        self.received.extend(frame.payload)
                        self.frame_sizes.append(len(frame.payload))
                    elif frame.type is FrameType.TERMINATE:
                        self.terminated = True
        except (asyncio.CancelledError, ConnectionError):
            raise
        except Exception:
            return

    async def connect(self, **kwargs: object) -> AudioSocketChannel:
        """Open the agent side of the call and return its media channel."""
        reader, writer = await asyncio.open_connection("127.0.0.1", self.port)
        channel = AudioSocketChannel(
            reader,
            writer,
            call_id=kwargs.pop("call_id", "test-call"),  # type: ignore[arg-type]
            wire_format=self._format,
            **kwargs,  # type: ignore[arg-type]
        )
        await channel.start()
        await self._connected.wait()
        return channel

    async def send_audio(self, pcm_at_wire_rate: bytes, *, real_time: bool = False) -> None:
        """Push caller audio, optionally paced like a real line."""
        assert self._writer is not None
        size = self._format.frame_bytes
        for offset in range(0, len(pcm_at_wire_rate), size):
            chunk = pcm_at_wire_rate[offset : offset + size]
            self._writer.write(audio_frame(chunk))
            await self._writer.drain()
            if real_time:
                await asyncio.sleep(0.02)

    async def hold_line(self) -> None:
        """Stream silence continuously, the way a real call does.

        A phone line never stops delivering audio — silence is still 50 frames
        a second. Endpointing counts frames, so a fake that sends a burst and
        then nothing leaves every turn permanently open.
        """
        if self._line is not None:
            return
        frame = b"\x00" * self._format.frame_bytes

        async def pump() -> None:
            while self._writer is not None:
                try:
                    self._writer.write(audio_frame(frame))
                    await self._writer.drain()
                except Exception:
                    return
                await asyncio.sleep(0.02)

        self._line = asyncio.create_task(pump())

    async def send_raw(self, data: bytes) -> None:
        """Write arbitrary bytes — used for malformed-input tests."""
        assert self._writer is not None
        self._writer.write(data)
        await self._writer.drain()

    async def send_dtmf(self, digit: str) -> None:
        assert self._writer is not None
        self._writer.write(Frame(FrameType.DTMF, digit.encode()).encode())
        await self._writer.drain()

    async def hang_up(self) -> None:
        """Caller hangs up: Asterisk closes the media socket."""
        if self._line is not None:
            self._line.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._line
            self._line = None
        if self._writer is not None:
            with contextlib.suppress(Exception):
                self._writer.close()
                await self._writer.wait_closed()
            self._writer = None

    async def stop(self) -> None:
        await self.hang_up()
        if self._server is not None:
            self._server.close()
            with contextlib.suppress(Exception):
                await self._server.wait_closed()
            self._server = None


def wire_tone(ms: int, *, rate: int = 8000, amplitude: int = 8000) -> bytes:
    """Synthetic caller speech at the wire rate, as signed linear."""
    import numpy as np

    samples = int(rate * ms / 1000)
    t = np.arange(samples) / rate
    tone = np.sin(2 * np.pi * 300 * t) + 0.5 * np.sin(2 * np.pi * 900 * t)
    return (tone / 1.5 * amplitude).astype("<i2").tobytes()


def wire_silence(ms: int, *, rate: int = 8000) -> bytes:
    return b"\x00" * (int(rate * ms / 1000) * 2)
