"""The AudioSocket wire protocol.

AudioSocket is Asterisk's simplest bidirectional media interface: a TCP stream
of type-length-value frames.

    ┌────────┬──────────────────┬─────────────────┐
    │ 1 byte │ 2 bytes (big-e.) │ payload         │
    │  type  │  payload length  │  0..65535 bytes │
    └────────┴──────────────────┴─────────────────┘

Asterisk opens the connection when the dialplan reaches ``AudioSocket()``,
sends a UUID frame identifying the call, then streams audio frames in both
directions until either side closes.

Parsing lives here as pure functions over bytes, separate from the socket, for
two reasons: a malformed frame from the network is a security concern and
deserves exhaustive unit tests, and the same parser is then used by the test
harness to *speak* the protocol back at us.

**Everything here is bounded.** A 2-byte length field is attacker-influenced
input; ``MAX_PAYLOAD_BYTES`` is enforced on both directions, and the incremental
decoder never buffers more than one maximum-size frame plus a header.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from enum import IntEnum

#: Header is type (1) + big-endian length (2).
HEADER_BYTES = 3

#: The length field is 16 bits, so this is a protocol maximum, not a policy.
MAX_PAYLOAD_BYTES = 0xFFFF

#: Longest single inbound media payload accepted.
#:
#: The previous value (3840) was derived from "Asterisk sends 20 ms per frame,
#: so 120 ms is generous". That premise is wrong: the protocol says nothing
#: about packet duration, and the first real call delivered a 7680-byte payload
#: — 480 ms of 8 kHz slin — which was rejected as malformed and killed the
#: media. A network packet size and an internal processing frame size are
#: different things, and this constant is the former.
#:
#: Expressed as a duration at the highest byte rate any supported wire format
#: produces, so it stays meaningful if a format is added: 16 kHz, 16-bit mono
#: is 32 bytes per millisecond. One second is roughly four times the largest
#: frame observed in practice and comfortably under the protocol's own 65535
#: ceiling, while bounding the decoder at 32 KB per connection — 256 KB across
#: the default eight-connection limit.
MAX_AUDIO_PAYLOAD_MS = 1_000
_MAX_BYTES_PER_MS = 32
MAX_REASONABLE_AUDIO_BYTES = MAX_AUDIO_PAYLOAD_MS * _MAX_BYTES_PER_MS  # 32_000


class FrameType(IntEnum):
    """AudioSocket frame types."""

    #: Peer is terminating the call.
    TERMINATE = 0x00
    #: 16-byte call identifier, sent once when the connection opens.
    UUID = 0x01
    #: A decoded keypad digit, where the Asterisk build supports it.
    DTMF = 0x03
    #: Signed linear PCM, 16-bit, mono, little-endian.
    AUDIO = 0x10
    #: Peer-reported error.
    ERROR = 0xFF


@dataclass(frozen=True, slots=True)
class Frame:
    type: FrameType
    payload: bytes = b""

    def encode(self) -> bytes:
        if len(self.payload) > MAX_PAYLOAD_BYTES:
            raise ValueError(f"payload exceeds {MAX_PAYLOAD_BYTES} bytes")
        return bytes([int(self.type)]) + len(self.payload).to_bytes(2, "big") + self.payload


class ProtocolError(ValueError):
    """The peer sent something that is not a valid AudioSocket frame."""


def audio_frame(pcm: bytes) -> bytes:
    return Frame(FrameType.AUDIO, pcm).encode()


def terminate_frame() -> bytes:
    return Frame(FrameType.TERMINATE).encode()


def uuid_frame(call_uuid: uuid.UUID | None = None) -> bytes:
    return Frame(FrameType.UUID, (call_uuid or uuid.uuid4()).bytes).encode()


def parse_uuid(payload: bytes) -> uuid.UUID:
    if len(payload) != 16:
        raise ProtocolError(f"UUID frame must be 16 bytes, got {len(payload)}")
    return uuid.UUID(bytes=payload)


def parse_dtmf(payload: bytes) -> str:
    """Decode a DTMF frame to a single digit.

    Asterisk sends the ASCII character. Anything outside the DTMF alphabet is
    rejected rather than passed upwards, because this value goes on to be used
    as structured input.
    """
    if len(payload) != 1:
        raise ProtocolError(f"DTMF frame must be 1 byte, got {len(payload)}")
    digit = payload.decode("ascii", errors="replace")
    if digit not in "0123456789*#ABCD":
        raise ProtocolError(f"not a DTMF digit: {digit!r}")
    return digit


class FrameDecoder:
    """Incremental decoder for a TCP byte stream.

    TCP gives a stream, not messages: one ``recv`` may contain half a frame or
    three and a half. Feed it whatever arrives and take whole frames out.
    """

    def __init__(self, *, max_payload: int = MAX_PAYLOAD_BYTES) -> None:
        if not 0 < max_payload <= MAX_PAYLOAD_BYTES:
            raise ValueError("max_payload must be within the protocol maximum")
        self._buffer = bytearray()
        self._max_payload = max_payload

    @property
    def buffered_bytes(self) -> int:
        return len(self._buffer)

    def feed(self, data: bytes) -> list[Frame]:
        """Add received bytes and return every complete frame now available."""
        self._buffer.extend(data)
        frames: list[Frame] = []
        while True:
            if len(self._buffer) < HEADER_BYTES:
                return frames
            raw_type = self._buffer[0]
            length = int.from_bytes(self._buffer[1:3], "big")
            if length > self._max_payload:
                # Refuse before allocating. The peer is malfunctioning or
                # hostile; either way the connection is no longer trustworthy.
                raise ProtocolError(f"frame declares {length} bytes, limit is {self._max_payload}")
            if len(self._buffer) < HEADER_BYTES + length:
                return frames
            payload = bytes(self._buffer[HEADER_BYTES : HEADER_BYTES + length])
            del self._buffer[: HEADER_BYTES + length]
            try:
                frame_type = FrameType(raw_type)
            except ValueError:
                # Unknown types are skipped rather than fatal: a newer Asterisk
                # adding a frame type must not take the call down. The bytes
                # are consumed above, so the stream stays aligned.
                continue
            frames.append(Frame(frame_type, payload))
