"""Audio transport protocols — the seam telephony will plug into.

``VoiceSession`` talks only to these two interfaces. Today they are backed by
the local microphone and speakers; in Milestone 4 an Asterisk external-media
transport implements the same two protocols and nothing in VAD, STT, TTS or the
conversation layer changes.

That is the whole point of the abstraction, so two rules are load-bearing:

* Frames are always **canonical format** (16 kHz mono s16le, 20 ms). A
  transport that receives 8 kHz μ-law converts at its own edge, exactly as the
  microphone converts from 48 kHz.
* ``stop_immediately`` must abandon audio that is already queued. Barge-in is
  only as fast as this call, so a transport that can merely "stop after the
  current buffer" is not an acceptable implementation.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol, runtime_checkable


@runtime_checkable
class AudioInput(Protocol):
    """A source of canonical-format audio frames."""

    async def start(self) -> None:
        """Begin capturing. Idempotent."""
        ...

    def frames(self) -> AsyncIterator[bytes]:
        """Yield 20 ms canonical frames until stopped.

        Must keep yielding during agent speech — barge-in detection depends on
        listening while talking, so a transport may not gate capture on
        playback.
        """
        ...

    async def stop(self) -> None:
        """Stop capturing and release the device. Idempotent."""
        ...

    @property
    def is_running(self) -> bool: ...


@runtime_checkable
class AudioOutput(Protocol):
    """A sink for canonical-format audio."""

    async def start(self) -> None:
        """Open the output device. Idempotent."""
        ...

    async def play(self, pcm: bytes) -> None:
        """Queue audio for playback. Returns once queued, not once heard."""
        ...

    async def wait_until_drained(self) -> None:
        """Block until queued audio has finished playing.

        Returns early if playback is stopped, so a barge-in never leaves a
        caller waiting on audio that was discarded.
        """
        ...

    async def stop_immediately(self) -> None:
        """Discard queued audio and silence output now. This is barge-in."""
        ...

    async def stop(self) -> None:
        """Close the device. Idempotent."""
        ...

    @property
    def is_playing(self) -> bool: ...
