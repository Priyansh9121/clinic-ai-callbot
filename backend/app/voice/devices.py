"""Local microphone and speaker transports (PortAudio via sounddevice).

Development transport for Milestone 3. Implements ``AudioInput`` /
``AudioOutput`` so Milestone 4 can substitute a telephony transport without
touching anything above.

``sounddevice`` calls back from a PortAudio thread, so both classes bridge that
thread to asyncio with a thread-safe queue and ``call_soon_threadsafe``. Doing
real work inside a PortAudio callback causes dropouts, so the callbacks only
move bytes.

Import of ``sounddevice`` is deferred to construction: the text-only deployment
must not need PortAudio installed to import the app.
"""

from __future__ import annotations

import asyncio
import contextlib
import queue
import threading
from collections.abc import AsyncIterator
from typing import Any

from app.core.logging import get_logger
from app.core.metrics import M, metrics
from app.speech.audio import resample_pcm16
from app.speech.providers import FRAME_BYTES, SAMPLE_RATE_HZ

logger = get_logger(__name__)

#: ~2 s of frames. Generous: the VAD consumer costs ~0.07 ms/frame, so the
#: queue should never fill. If it does, something upstream has stalled and the
#: overrun metric is the signal.
DEFAULT_INPUT_QUEUE_FRAMES = 100

#: Playback ring depth in frames (~1 s). Deep enough to survive scheduler
#: jitter, shallow enough that discarding it on barge-in is near-instant.
DEFAULT_OUTPUT_QUEUE_FRAMES = 50


def list_devices() -> list[dict[str, Any]]:
    """Enumerate audio devices for the CLI. Empty list if PortAudio is absent."""
    try:
        import sounddevice as sd
    except Exception as exc:  # pragma: no cover - depends on host audio stack
        logger.warning("audio.enumerate_failed", error=type(exc).__name__)
        return []
    devices = []
    for index, device in enumerate(sd.query_devices()):
        devices.append(
            {
                "index": index,
                "name": device["name"],
                "inputs": device["max_input_channels"],
                "outputs": device["max_output_channels"],
                "default_samplerate": int(device["default_samplerate"]),
            }
        )
    return devices


def default_devices() -> tuple[int | None, int | None]:
    try:
        import sounddevice as sd

        default = sd.default.device
        return default[0], default[1]
    except Exception:  # pragma: no cover - depends on host audio stack
        return None, None


class MicrophoneAudioInput:
    """Captures canonical 20 ms frames from a local input device.

    Opens at 16 kHz where the device allows it (no resampling at all), and
    falls back to the device's native rate with a single soxr conversion.
    """

    def __init__(
        self,
        *,
        device: int | str | None = None,
        queue_frames: int = DEFAULT_INPUT_QUEUE_FRAMES,
    ) -> None:
        self._device = device
        self._stream: Any | None = None
        self._raw: queue.Queue[bytes] = queue.Queue(maxsize=queue_frames)
        self._capture_rate = SAMPLE_RATE_HZ
        self._running = False
        self._overruns = 0
        self._partial = bytearray()

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def capture_rate(self) -> int:
        return self._capture_rate

    @property
    def overruns(self) -> int:
        return self._overruns

    def _callback(self, indata, frames, time_info, status) -> None:
        """PortAudio thread. Moves bytes only — no processing here."""
        if status:
            # Input overflow reported by the driver itself.
            metrics.increment(M.AUDIO_INPUT_OVERRUN, labels={"source": "driver"})
        try:
            self._raw.put_nowait(bytes(indata))
        except queue.Full:
            # Drop the oldest frame: recent audio matters more for
            # responsiveness, and the consumer being this far behind is
            # already a fault worth surfacing.
            self._overruns += 1
            metrics.increment(M.AUDIO_INPUT_OVERRUN, labels={"source": "queue"})
            try:
                self._raw.get_nowait()
                self._raw.put_nowait(bytes(indata))
            except (queue.Empty, queue.Full):  # pragma: no cover - race only
                pass

    async def start(self) -> None:
        if self._running:
            return
        import sounddevice as sd

        # Prefer opening at the canonical rate so no resampling is needed at
        # all; fall back to the device's native rate if it refuses.
        for rate in (SAMPLE_RATE_HZ, None):
            try:
                target = rate or int(sd.query_devices(self._device, "input")["default_samplerate"])
                self._stream = sd.RawInputStream(
                    samplerate=target,
                    channels=1,
                    dtype="int16",
                    blocksize=target * 20 // 1000,
                    device=self._device,
                    callback=self._callback,
                )
                self._stream.start()
                self._capture_rate = target
                break
            except Exception as exc:
                self._stream = None
                if rate is None:
                    raise RuntimeError(f"could not open microphone: {exc}") from exc

        self._running = True
        logger.info(
            "audio.input_started",
            device=str(self._device),
            capture_rate=self._capture_rate,
            resampling=self._capture_rate != SAMPLE_RATE_HZ,
        )

    async def frames(self) -> AsyncIterator[bytes]:
        """Yield canonical 20 ms frames until stopped."""
        loop = asyncio.get_running_loop()
        while self._running:
            try:
                raw = await loop.run_in_executor(None, self._raw.get, True, 0.25)
            except queue.Empty:  # pragma: no cover - timing dependent
                continue
            if raw is None:
                break
            pcm = (
                raw
                if self._capture_rate == SAMPLE_RATE_HZ
                else resample_pcm16(raw, self._capture_rate, SAMPLE_RATE_HZ)
            )
            # Resampling rarely lands on an exact frame boundary, so buffer and
            # emit only whole frames — the VAD requires exact sizes.
            self._partial.extend(pcm)
            while len(self._partial) >= FRAME_BYTES:
                yield bytes(self._partial[:FRAME_BYTES])
                del self._partial[:FRAME_BYTES]

    async def stop(self) -> None:
        if not self._running:
            return
        self._running = False
        stream, self._stream = self._stream, None
        if stream is not None:
            await asyncio.to_thread(self._close_stream, stream)
        # Unblock a consumer waiting on the queue.
        with contextlib.suppress(queue.Full):
            self._raw.put_nowait(None)  # type: ignore[arg-type]
        logger.info("audio.input_stopped", overruns=self._overruns)

    @staticmethod
    def _close_stream(stream: Any) -> None:
        try:
            stream.stop()
            stream.close()
        except Exception as exc:  # pragma: no cover - device teardown races
            logger.warning("audio.input_close_failed", error=type(exc).__name__)


class SpeakerAudioOutput:
    """Plays canonical PCM to a local output device, cancellable immediately.

    Playback is a ring of frames consumed by the PortAudio callback. Barge-in
    clears the ring, so output goes silent within one callback period (~20 ms)
    rather than after whatever was already buffered.
    """

    def __init__(
        self,
        *,
        device: int | str | None = None,
        queue_frames: int = DEFAULT_OUTPUT_QUEUE_FRAMES,
    ) -> None:
        self._device = device
        self._stream: Any | None = None
        self._chunks: queue.Queue[bytes] = queue.Queue(maxsize=queue_frames)
        self._lock = threading.Lock()
        self._pending = bytearray()
        self._running = False
        self._playing = False
        self._drained = asyncio.Event()
        self._drained.set()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._underruns = 0

    @property
    def is_playing(self) -> bool:
        return self._playing

    @property
    def underruns(self) -> int:
        return self._underruns

    def _callback(self, outdata, frames, time_info, status) -> None:
        """PortAudio thread. Fills the buffer from the ring, else silence."""
        wanted = frames * 2
        with self._lock:
            while len(self._pending) < wanted:
                try:
                    self._pending.extend(self._chunks.get_nowait())
                except queue.Empty:
                    break
            if len(self._pending) >= wanted:
                outdata[:] = bytes(self._pending[:wanted])
                del self._pending[:wanted]
                still_playing = True
            else:
                # Underrun or genuinely finished: emit silence either way, so
                # the device never repeats a stale buffer.
                available = len(self._pending)
                outdata[:available] = bytes(self._pending)
                outdata[available:] = b"\x00" * (wanted - available)
                self._pending.clear()
                still_playing = False
                if available:
                    self._underruns += 1

        if not still_playing and self._playing:
            self._playing = False
            self._signal_drained()

    def _signal_drained(self) -> None:
        loop = self._loop
        if loop is not None and not loop.is_closed():
            loop.call_soon_threadsafe(self._drained.set)

    async def start(self) -> None:
        if self._running:
            return
        import sounddevice as sd

        self._loop = asyncio.get_running_loop()
        self._stream = sd.RawOutputStream(
            samplerate=SAMPLE_RATE_HZ,
            channels=1,
            dtype="int16",
            blocksize=SAMPLE_RATE_HZ * 20 // 1000,
            device=self._device,
            callback=self._callback,
        )
        self._stream.start()
        self._running = True
        logger.info("audio.output_started", device=str(self._device))

    async def play(self, pcm: bytes) -> None:
        """Queue audio. Blocks only if the ring is full (natural backpressure)."""
        if not pcm or not self._running:
            return
        self._drained.clear()
        self._playing = True
        for offset in range(0, len(pcm), FRAME_BYTES):
            chunk = pcm[offset : offset + FRAME_BYTES]
            while True:
                try:
                    self._chunks.put_nowait(chunk)
                    break
                except queue.Full:
                    # Ring full: wait for the callback to drain rather than
                    # growing memory without bound.
                    await asyncio.sleep(0.005)
                    if not self._running:
                        return

    async def wait_until_drained(self) -> None:
        if not self._running:
            return
        await self._drained.wait()

    async def stop_immediately(self) -> None:
        """Discard everything queued and go silent. This is barge-in."""
        with self._lock:
            self._pending.clear()
            while True:
                try:
                    self._chunks.get_nowait()
                except queue.Empty:
                    break
        self._playing = False
        self._drained.set()

    async def stop(self) -> None:
        if not self._running:
            return
        await self.stop_immediately()
        self._running = False
        stream, self._stream = self._stream, None
        if stream is not None:
            await asyncio.to_thread(self._close_stream, stream)
        logger.info("audio.output_stopped", underruns=self._underruns)

    @staticmethod
    def _close_stream(stream: Any) -> None:
        try:
            stream.stop()
            stream.close()
        except Exception as exc:  # pragma: no cover - device teardown races
            logger.warning("audio.output_close_failed", error=type(exc).__name__)
