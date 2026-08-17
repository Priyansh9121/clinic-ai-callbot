"""Telephony audio transport — the adapter that makes a phone call look like a
microphone.

Implements the Milestone 3 ``AudioInput`` / ``AudioOutput`` protocols over an
AudioSocket connection, so ``VoiceSession``, VAD, endpointing, STT and TTS run
unchanged. Nothing in this module knows what an appointment is.

Two things make telephone audio different from a sound card, and both are
handled at this edge so that nothing above has to care:

**Rate and codec.** A phone leg is 8 kHz. AudioSocket delivers whatever the
channel format is — slin at 8 kHz for a G.711 call, slin16 if the dialplan
raises it. Conversion to and from the canonical 16 kHz happens here, using the
codec helpers in ``app/speech/audio.py`` and nowhere else.

**Pacing.** A sound card consumes audio at exactly real time; a TCP socket
accepts it as fast as Python can write. Dumping a 4-second reply into the
socket in 3 ms would leave four seconds of speech buffered inside Asterisk,
which does two bad things: latency, and — much worse — it makes barge-in
impossible, because ``stop_immediately`` cannot recall audio the kernel has
already sent. So outbound audio is metered one 20 ms frame per 20 ms. **Pacing
is a barge-in correctness requirement, not an audio-quality nicety**, and it
bounds the amount of un-recallable speech to roughly one frame.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import AsyncIterator, Callable
from enum import StrEnum
from typing import Any

from app.core.logging import get_logger
from app.core.metrics import M, metrics
from app.speech.audio import (
    alaw_to_pcm16,
    pcm16_to_alaw,
    pcm16_to_ulaw,
    ulaw_to_pcm16,
)
from app.speech.providers import FRAME_BYTES, FRAME_MS, SAMPLE_RATE_HZ
from app.telephony.audiosocket import (
    MAX_REASONABLE_AUDIO_BYTES,
    FrameDecoder,
    FrameType,
    ProtocolError,
    audio_frame,
    parse_dtmf,
    parse_uuid,
    terminate_frame,
)

logger = get_logger(__name__)

FRAME_SECONDS = FRAME_MS / 1000

#: Inbound queue depth. ~1 s: deep enough to ride out a scheduling hiccup,
#: shallow enough that a stalled consumer is noticed rather than hidden.
DEFAULT_INBOUND_FRAMES = 50

#: Outbound queue depth. This is *not* a playback buffer — the pacer meters
#: frames out in real time — it only bounds how much synthesised speech may
#: wait. ~5 s, which is longer than any single approved reply.
DEFAULT_OUTBOUND_FRAMES = 250

#: If the pacer falls further behind than this it stops trying to catch up and
#: resynchronises. Catching up would mean sending a burst, which is the thing
#: pacing exists to prevent.
_PACING_RESYNC_MS = 100


class TelephonyFormat(StrEnum):
    """Wire format of the media leg.

    AudioSocket always carries signed linear; the G.711 members exist for an
    RTP/ExternalMedia path, where the payload is the negotiated codec itself.
    """

    #: Signed linear 16-bit, 8 kHz. The AudioSocket default.
    SLIN = "slin"
    #: Signed linear 16-bit, 16 kHz. Matches the canonical rate exactly, so no
    #: resampling is needed — worth configuring where the dialplan allows it.
    SLIN16 = "slin16"
    #: G.711 μ-law, 8 kHz.
    PCMU = "pcmu"
    #: G.711 A-law, 8 kHz.
    PCMA = "pcma"

    @property
    def sample_rate(self) -> int:
        return 16_000 if self is TelephonyFormat.SLIN16 else 8_000

    @property
    def asterisk_name(self) -> str:
        """What Asterisk calls this format, for ARI's `formats` parameter.

        Must be sent on every originate. Left unspecified, ARI picks the best
        format the channel can do — which on a modern Asterisk is slin192, so
        every 20 ms packet arrives carrying 7680 bytes that we then read as
        480 ms of 8 kHz audio.
        """
        return {
            TelephonyFormat.SLIN: "slin",
            TelephonyFormat.SLIN16: "slin16",
            TelephonyFormat.PCMU: "ulaw",
            TelephonyFormat.PCMA: "alaw",
        }[self]

    @property
    def bytes_per_sample(self) -> int:
        """1 for the G.711 companded formats, 2 for signed linear."""
        return 1 if self in (TelephonyFormat.PCMU, TelephonyFormat.PCMA) else 2

    @property
    def frame_bytes(self) -> int:
        """Bytes in one 20 ms frame on the wire."""
        samples = self.sample_rate * FRAME_MS // 1000
        return samples * self.bytes_per_sample

    def to_pcm16(self, data: bytes) -> bytes:
        if self is TelephonyFormat.PCMU:
            return ulaw_to_pcm16(data)
        if self is TelephonyFormat.PCMA:
            return alaw_to_pcm16(data)
        return data

    def from_pcm16(self, pcm: bytes) -> bytes:
        if self is TelephonyFormat.PCMU:
            return pcm16_to_ulaw(pcm)
        if self is TelephonyFormat.PCMA:
            return pcm16_to_alaw(pcm)
        return pcm


class _StreamResampler:
    """Stateful rate conversion, or a pass-through when rates match.

    Stateful rather than one-shot-per-frame on measurement: resampling each
    20 ms frame independently puts a filter discontinuity at every frame
    boundary — 50 a second — for a spectral error of 0.327 against 0.000 for
    the streaming resampler. It costs 0.002 ms per frame.
    """

    def __init__(self, from_rate: int, to_rate: int, *, quality: str = "HQ") -> None:
        self._passthrough = from_rate == to_rate
        self._from_rate = from_rate
        self._to_rate = to_rate
        self._quality = quality
        self._stream = None
        if not self._passthrough:
            import numpy as np
            import soxr

            self._np = np
            self._soxr = soxr
            self._stream = self._new_stream()

    def _new_stream(self) -> Any:
        return self._soxr.ResampleStream(
            self._from_rate, self._to_rate, 1, quality=self._quality, dtype="float32"
        )

    def _to_bytes(self, out: Any) -> bytes:
        if out.size == 0:
            return b""
        return (self._np.clip(out, -1.0, 1.0) * 32767.0).astype("<i2").tobytes()

    def convert(self, pcm: bytes) -> bytes:
        # `_stream is None` is the same guard flush() already uses: it is only
        # unset when passthrough, so this states the invariant rather than
        # relying on the two flags never drifting apart.
        if self._passthrough or self._stream is None or not pcm:
            return pcm
        samples = self._np.frombuffer(pcm, dtype="<i2").astype(self._np.float32) / 32768.0
        return self._to_bytes(self._stream.resample_chunk(samples))

    def flush(self) -> bytes:
        """Emit the samples held in the filter and start a fresh stream.

        Measured: at HQ the resampler holds ~100 ms back. Without an explicit
        flush at the end of an utterance that tail sits in the filter until the
        *next* reply pushes it out, so every sentence loses its ending and the
        following one starts with a fragment of the last. Reset rather than
        reuse, because soxr refuses further input after a final chunk.
        """
        if self._passthrough or self._stream is None:
            return b""
        tail = self._to_bytes(
            self._stream.resample_chunk(self._np.zeros(0, dtype=self._np.float32), last=True)
        )
        self._stream = self._new_stream()
        return tail

    def reset(self) -> None:
        """Discard filter state without emitting it. Used on barge-in."""
        if not self._passthrough:
            self._stream = self._new_stream()


class AudioSocketChannel:
    """One AudioSocket connection: one call, both directions.

    Owns the socket and the two pumps. ``AudioSocketInput`` and
    ``AudioSocketOutput`` are thin views onto it, so that ``VoiceSession`` sees
    the two protocols it expects while the socket has a single owner.
    """

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        *,
        call_id: str,
        wire_format: TelephonyFormat = TelephonyFormat.SLIN,
        inbound_frames: int = DEFAULT_INBOUND_FRAMES,
        outbound_frames: int = DEFAULT_OUTBOUND_FRAMES,
        resample_quality: str = "HQ",
    ) -> None:
        self._reader = reader
        self._writer = writer
        self._call_id = call_id
        self._format = wire_format

        # Carries (enqueued_at, frame). The timestamp is internal: frames()
        # still yields bare bytes, so nothing above this class changes.
        self._inbound: asyncio.Queue[tuple[float, bytes]] = asyncio.Queue(maxsize=inbound_frames)
        self._outbound: asyncio.Queue[bytes] = asyncio.Queue(maxsize=outbound_frames)
        self._pending = bytearray()  # partial canonical frame from the wire
        self._outbound_pending = bytearray()  # partial wire frame awaiting a full 20 ms
        self._resample_quality = resample_quality

        self._up = _StreamResampler(
            self._format.sample_rate, SAMPLE_RATE_HZ, quality=resample_quality
        )
        self._down = _StreamResampler(
            SAMPLE_RATE_HZ, self._format.sample_rate, quality=resample_quality
        )

        self._decoder = FrameDecoder(max_payload=MAX_REASONABLE_AUDIO_BYTES)
        self._read_task: asyncio.Task[None] | None = None
        self._write_task: asyncio.Task[None] | None = None
        self._closed = asyncio.Event()
        self._uuid_seen = asyncio.Event()
        self._drained = asyncio.Event()
        self._drained.set()
        #: Set when the first audio of a reply enters an idle transport, and
        #: cleared when that reply's first frame is actually written. The gap
        #: is the transport's own contribution to what the caller waits for —
        #: resampler batching, the outbound queue and the pacer — which no
        #: measurement above this layer can see.
        self._reply_started_at: float | None = None
        #: Inbound arrival cadence, measured per decoded wire frame. A phone
        #: line delivers 20 ms every 20 ms; batching shows up here as a mean
        #: far below 20 or an outlying max, and distinguishes a slow endpointer
        #: from a transport handing it audio in bursts.
        self._last_wire_frame_at: float | None = None
        self._arrival_count = 0
        self._arrival_total_ms = 0.0
        self._arrival_max_ms = 0.0
        #: How long a canonical frame waits between being decoded off the
        #: socket and the voice session taking it. endpoint_latency_ms is timed
        #: from when the VAD saw silence, which is this much after the caller
        #: actually stopped.
        self._dwell_count = 0
        self._dwell_total_ms = 0.0
        self._dwell_max_ms = 0.0
        #: Milliseconds from first audio in to first byte on the wire, for the
        #: most recent reply. None until a reply has been sent.
        self.last_reply_wire_latency_ms: float | None = None
        self._running = False
        self._sending = False
        #: Frame delivery to the voice session. Separate from the socket's own
        #: lifetime: a session may need to stop listening while the channel
        #: stays open to transmit a goodbye.
        self._input_open = True
        self._wire_bytes = 0
        self._read_started_at: float | None = None

        self._on_hangup: Callable[[], None] | None = None
        self._on_dtmf: Callable[[str], None] | None = None
        self._peer_uuid: str | None = None

        self.inbound_frames_seen = 0
        self.outbound_frames_sent = 0
        self.overruns = 0

    # --- Lifecycle ---------------------------------------------------------
    @property
    def call_id(self) -> str:
        return self._call_id

    @property
    def peer_uuid(self) -> str | None:
        return self._peer_uuid

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def inbound_arrival_mean_ms(self) -> float | None:
        """Mean gap between decoded inbound wire frames. ~20 ms on a real line."""
        if not self._arrival_count:
            return None
        return self._arrival_total_ms / self._arrival_count

    @property
    def inbound_arrival_max_ms(self) -> float | None:
        """Largest gap seen. A high max with a low mean is bursty delivery."""
        return self._arrival_max_ms if self._arrival_count else None

    @property
    def inbound_dwell_mean_ms(self) -> float | None:
        """Mean socket-to-consumer wait. Time the endpointer is behind by."""
        if not self._dwell_count:
            return None
        return self._dwell_total_ms / self._dwell_count

    @property
    def inbound_dwell_max_ms(self) -> float | None:
        return self._dwell_max_ms if self._dwell_count else None

    @property
    def is_sending(self) -> bool:
        return self._sending or not self._outbound.empty()

    def on_hangup(self, callback: Callable[[], None]) -> None:
        self._on_hangup = callback

    def on_dtmf(self, callback: Callable[[str], None]) -> None:
        self._on_dtmf = callback

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._read_task = asyncio.create_task(self._read_loop())
        self._write_task = asyncio.create_task(self._write_loop())

    async def close(self, *, notify_peer: bool = True) -> None:
        """Stop both pumps and release the socket. Idempotent."""
        if not self._running and self._closed.is_set():
            return
        self._running = False
        self._closed.set()
        if notify_peer:
            with contextlib.suppress(Exception):
                self._writer.write(terminate_frame())
                await self._writer.drain()
        for task in (self._read_task, self._write_task):
            if task is not None and not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
        with contextlib.suppress(Exception):
            self._writer.close()
            await self._writer.wait_closed()
        self._drained.set()

    # --- Inbound -----------------------------------------------------------
    async def _read_loop(self) -> None:
        """Decode the wire, convert to canonical frames, feed the queue."""
        try:
            while self._running:
                data = await self._reader.read(4096)
                if self._read_started_at is None:
                    self._read_started_at = time.monotonic()
                self._wire_bytes += len(data)
                if not data:
                    # The peer closed its side. Whether that is the caller
                    # hanging up or Asterisk giving up on us is not visible
                    # here, but the frame count is: zero means it never sent
                    # audio at all, which is a very different problem from it
                    # stopping part way through.
                    logger.info(
                        "telephony.media_closed",
                        call_id=self._call_id,
                        reason="peer_eof",
                        inbound_frames=self.inbound_frames_seen,
                    )
                    break
                try:
                    frames = self._decoder.feed(data)
                except ProtocolError as exc:
                    # A peer that cannot frame correctly cannot be trusted to
                    # keep the stream aligned, so the call's media ends here.
                    metrics.increment(M.TELEPHONY_MEDIA_ERROR, labels={"kind": "protocol"})
                    logger.warning(
                        "telephony.protocol_error", call_id=self._call_id, error=str(exc)
                    )
                    break
                for frame in frames:
                    if not self._handle_frame(frame.type, frame.payload):
                        return
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            metrics.increment(M.TELEPHONY_MEDIA_ERROR, labels={"kind": "read"})
            logger.warning(
                "telephony.media_read_failed", call_id=self._call_id, error=type(exc).__name__
            )
        finally:
            # The listen loop ends when this does, so this is where a call that
            # went quiet after the greeting is explained.
            # The measured wire rate against what the configured format
            # implies. A ratio far from 1.0 means Asterisk is sending a
            # different format from the one we decode, which is otherwise
            # invisible: the socket just delivers more bytes than it should and
            # every duration computed from them is wrong by that factor.
            elapsed = time.monotonic() - self._read_started_at if self._read_started_at else 0.0
            expected = self._format.sample_rate * self._format.bytes_per_sample
            measured = self._wire_bytes / elapsed if elapsed > 0.1 else 0.0
            logger.info(
                "telephony.media_read_stopped",
                call_id=self._call_id,
                inbound_frames=self.inbound_frames_seen,
                overruns=self.overruns,
                wire_bytes_per_s=round(measured),
                expected_bytes_per_s=expected,
                rate_ratio=round(measured / expected, 2) if expected else 0.0,
            )
            self._running = False
            self._closed.set()
            self._notify_hangup()

    def _handle_frame(self, frame_type: FrameType, payload: bytes) -> bool:
        """Process one decoded frame. Returns False to end the read loop."""
        if frame_type is FrameType.AUDIO:
            self._ingest_audio(payload)
            return True
        if frame_type is FrameType.UUID:
            with contextlib.suppress(ProtocolError):
                self._peer_uuid = str(parse_uuid(payload))
            self._uuid_seen.set()
            return True
        if frame_type is FrameType.DTMF:
            try:
                digit = parse_dtmf(payload)
            except ProtocolError:
                metrics.increment(M.TELEPHONY_MEDIA_ERROR, labels={"kind": "dtmf"})
                return True
            metrics.increment(M.TELEPHONY_DTMF)
            if self._on_dtmf is not None:
                self._on_dtmf(digit)
            return True
        if frame_type is FrameType.TERMINATE:
            logger.info("telephony.peer_terminated", call_id=self._call_id)
            return False
        if frame_type is FrameType.ERROR:
            metrics.increment(M.TELEPHONY_MEDIA_ERROR, labels={"kind": "peer"})
            logger.warning("telephony.peer_error", call_id=self._call_id)
            return True
        return True

    def _ingest_audio(self, payload: bytes) -> None:
        # A 16-bit format cannot deliver half a sample. Checked here rather
        # than in the decoder, which is format-agnostic: G.711 is one byte per
        # sample and an odd length is perfectly valid there. Without this the
        # odd byte reaches numpy, which raises "buffer size must be a multiple
        # of element size" and ends the call as an unexplained read failure.
        if self._format.bytes_per_sample == 2 and len(payload) % 2:
            metrics.increment(M.TELEPHONY_MEDIA_ERROR, labels={"kind": "odd_pcm16"})
            logger.warning(
                "telephony.audio_frame_truncated", call_id=self._call_id, bytes=len(payload)
            )
            return
        now = time.monotonic()
        if self._last_wire_frame_at is not None:
            gap_ms = (now - self._last_wire_frame_at) * 1000
            self._arrival_count += 1
            self._arrival_total_ms += gap_ms
            self._arrival_max_ms = max(self._arrival_max_ms, gap_ms)
        self._last_wire_frame_at = now

        pcm = self._up.convert(self._format.to_pcm16(payload))
        if not pcm:
            return
        self._pending.extend(pcm)
        while len(self._pending) >= FRAME_BYTES:
            frame = bytes(self._pending[:FRAME_BYTES])
            del self._pending[:FRAME_BYTES]
            self.inbound_frames_seen += 1
            metrics.increment(M.TELEPHONY_INBOUND_FRAMES)
            if self._inbound.full():
                # Drop the oldest: on a live call, stale audio is worthless and
                # unbounded growth is dangerous. Same policy as the microphone.
                with contextlib.suppress(asyncio.QueueEmpty):
                    self._inbound.get_nowait()
                self.overruns += 1
                metrics.increment(M.TELEPHONY_AUDIO_OVERRUN)
            with contextlib.suppress(asyncio.QueueFull):
                self._inbound.put_nowait((time.monotonic(), frame))

    def stop_input(self) -> None:
        """Stop delivering frames without closing the socket.

        ``VoiceSession.stop()`` relies on the frame source ending to break its
        listen loop. On a microphone that happens because the device closes;
        here the socket is shared with the output and owned by the call, so
        delivery is what stops. Without this a caller hangup left the listen
        loop parked on an empty queue and the call never ended.
        """
        self._input_open = False

    async def frames(self) -> AsyncIterator[bytes]:
        """Canonical 20 ms frames until the call's media ends."""
        while self._input_open and (self._running or not self._inbound.empty()):
            try:
                enqueued_at, frame = await asyncio.wait_for(self._inbound.get(), timeout=0.05)
            except TimeoutError:
                if self._closed.is_set() and self._inbound.empty():
                    return
                continue
            dwell_ms = (time.monotonic() - enqueued_at) * 1000
            self._dwell_count += 1
            self._dwell_total_ms += dwell_ms
            self._dwell_max_ms = max(self._dwell_max_ms, dwell_ms)
            if not self._input_open:
                return
            yield frame

    # --- Outbound ----------------------------------------------------------
    async def send(self, pcm: bytes) -> None:
        """Convert canonical PCM to wire frames and queue them.

        Conversion happens **here**, not in the pacer. ``soxr``'s streaming
        resampler batches: at HQ it returns nothing for five 20 ms frames and
        then 104 ms in one blob. Converting inside the pacer would therefore
        emit one oversized wire frame every ~100 ms — bursts, which is exactly
        what pacing exists to prevent. Converting on the way *in* lets the
        queue hold uniform 20 ms wire frames whatever the resampler does, so
        the pacer stays exact at any quality setting.
        """
        if not self._running:
            return
        # Idle until now means this is the first audio of a reply. Timed from
        # here rather than from the pacer, because the resampler holds audio
        # back before the queue ever sees it and that wait is the caller's too.
        if self._drained.is_set():
            self._reply_started_at = time.monotonic()
        self._drained.clear()
        self._enqueue_wire(self._format.from_pcm16(self._down.convert(pcm)))

    def _enqueue_wire(self, wire: bytes, *, pad_final: bool = False) -> None:
        """Cut wire-format bytes into exact 20 ms frames and queue them."""
        if wire:
            self._outbound_pending.extend(wire)
        frame_bytes = self._format.frame_bytes
        if pad_final and self._outbound_pending:
            shortfall = -len(self._outbound_pending) % frame_bytes
            if shortfall:
                self._outbound_pending.extend(self._format.from_pcm16(b"\x00" * shortfall))
        while len(self._outbound_pending) >= frame_bytes:
            frame = bytes(self._outbound_pending[:frame_bytes])
            del self._outbound_pending[:frame_bytes]
            try:
                self._outbound.put_nowait(frame)
            except asyncio.QueueFull:
                # Never block the conversation on the wire. Dropping the tail
                # of an over-long reply is better than stalling the turn.
                metrics.increment(M.TELEPHONY_AUDIO_OVERRUN)
                logger.warning("telephony.outbound_full", call_id=self._call_id)
                return

    def flush_outbound(self) -> None:
        """Push the resampler's held-back tail out. Ends one utterance."""
        self._enqueue_wire(self._format.from_pcm16(self._down.flush()), pad_final=True)

    async def _write_loop(self) -> None:
        """Meter outbound frames at real time — one 20 ms frame per 20 ms."""
        next_send = time.monotonic()
        try:
            while self._running:
                try:
                    frame = await asyncio.wait_for(self._outbound.get(), timeout=0.05)
                except TimeoutError:
                    if self._outbound.empty():
                        self._sending = False
                        self._drained.set()
                    continue

                now = time.monotonic()
                if now - next_send > _PACING_RESYNC_MS / 1000:
                    # Idle, or we lost the CPU. Start a fresh schedule rather
                    # than firing a catch-up burst at Asterisk.
                    next_send = now
                delay = next_send - now
                if delay > 0:
                    await asyncio.sleep(delay)

                if not self._running:
                    return
                self._sending = True
                if self._reply_started_at is not None:
                    self.last_reply_wire_latency_ms = (
                        time.monotonic() - self._reply_started_at
                    ) * 1000
                    self._reply_started_at = None
                    metrics.observe(M.TELEPHONY_REPLY_WIRE_LATENCY, self.last_reply_wire_latency_ms)
                # Already wire-format and exactly one frame: see send().
                self._writer.write(audio_frame(frame))
                await self._writer.drain()
                self.outbound_frames_sent += 1
                metrics.increment(M.TELEPHONY_OUTBOUND_FRAMES)
                next_send += FRAME_SECONDS
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            metrics.increment(M.TELEPHONY_MEDIA_ERROR, labels={"kind": "write"})
            logger.warning(
                "telephony.media_write_failed", call_id=self._call_id, error=type(exc).__name__
            )
            self._running = False
            self._closed.set()
        finally:
            self._sending = False
            self._drained.set()

    def discard_outbound(self) -> int:
        """Drop everything queued but not yet transmitted. This is barge-in.

        Only queued audio can be recalled. Whatever the pacer has already
        written is in the kernel and inside Asterisk, and will be heard — which
        is precisely why the pacer never runs ahead by more than a frame.
        """
        dropped = 0
        while True:
            try:
                self._outbound.get_nowait()
                dropped += 1
            except asyncio.QueueEmpty:
                break
        self._outbound_pending.clear()
        # The resampler is holding up to ~100 ms of the abandoned utterance in
        # its filter. Left alone it would prepend the tail of the interrupted
        # sentence to the next reply — a stale-audio leak that survives every
        # check above it, because by then the bytes look like the new turn's.
        self._down.reset()
        self._sending = False
        self._drained.set()
        return dropped

    async def wait_for_uuid(self, *, limit_seconds: float) -> str | None:
        """Wait for Asterisk's opening UUID frame, which names the call.

        This is how a media connection is matched to the ARI channel that
        created it. Audio arriving first is queued rather than dropped, so a
        caller who is already speaking is not clipped by the correlation.
        """
        with contextlib.suppress(TimeoutError):
            async with asyncio.timeout(limit_seconds):
                await self._uuid_seen.wait()
        return self._peer_uuid

    async def wait_drained(self) -> None:
        self.flush_outbound()
        await self._drained.wait()

    def _notify_hangup(self) -> None:
        if self._on_hangup is not None:
            callback, self._on_hangup = self._on_hangup, None
            callback()


class AudioSocketInput:
    """``AudioInput`` over a telephone call."""

    def __init__(self, channel: AudioSocketChannel) -> None:
        self._channel = channel

    @property
    def is_running(self) -> bool:
        return self._channel.is_running

    async def start(self) -> None:
        await self._channel.start()

    def frames(self) -> AsyncIterator[bytes]:
        return self._channel.frames()

    async def stop(self) -> None:
        # Stop delivering frames, but leave the socket open: it is shared with
        # the output, and a final "goodbye" or a transfer may still need it.
        # The call layer owns closing the channel.
        self._channel.stop_input()


class AudioSocketOutput:
    """``AudioOutput`` over a telephone call."""

    def __init__(self, channel: AudioSocketChannel) -> None:
        self._channel = channel
        self._discarded_frames = 0

    @property
    def is_playing(self) -> bool:
        return self._channel.is_sending

    @property
    def discarded_frames(self) -> int:
        return self._discarded_frames

    async def start(self) -> None:
        await self._channel.start()

    async def play(self, pcm: bytes) -> None:
        await self._channel.send(pcm)

    async def wait_until_drained(self) -> None:
        await self._channel.wait_drained()

    async def stop_immediately(self) -> None:
        self._discarded_frames += self._channel.discard_outbound()

    async def stop(self) -> None:
        self._channel.discard_outbound()
