"""Wire packet sizes, as Asterisk actually sends them.

Written after the first real call to extension 700. Everything up to media
worked — SIP, ARI, Stasis, the bridge, the origination, the VM reaching the Mac
on 8090 — and then:

    telephony.protocol_error  error='frame declares 7680 bytes, limit is 3840'

The limit was derived from "Asterisk sends 20 ms per frame, so 120 ms is
generous". The protocol makes no such promise. 7680 bytes is 480 ms of 8 kHz
slin, and a real Asterisk 20.6.0 sent it on the first packet.

A network packet size and an internal processing frame size are different
things, and conflating them cost a call.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest

from app.telephony.audiosocket import (
    HEADER_BYTES,
    MAX_PAYLOAD_BYTES,
    MAX_REASONABLE_AUDIO_BYTES,
    FrameDecoder,
    FrameType,
    ProtocolError,
    audio_frame,
    parse_dtmf,
    parse_uuid,
)
from app.telephony.transport import FRAME_BYTES, AudioSocketInput
from tests.fixtures.fake_telephony import FakeAsterisk

#: The exact payload size the live call was killed by.
OBSERVED_LIVE_PAYLOAD = 7_680


@pytest.fixture
async def pbx():
    fake = FakeAsterisk()
    await fake.start()
    yield fake
    await fake.stop()


def _pcm(nbytes: int) -> bytes:
    """Deterministic non-repeating PCM, so reordering is detectable."""
    return bytes((i * 7 + 3) % 256 for i in range(nbytes))


# --- The live failure -------------------------------------------------------
def test_the_payload_that_killed_the_first_real_call_is_accepted() -> None:
    decoder = FrameDecoder(max_payload=MAX_REASONABLE_AUDIO_BYTES)
    pcm = _pcm(OBSERVED_LIVE_PAYLOAD)

    frames = decoder.feed(audio_frame(pcm))

    assert len(frames) == 1
    assert frames[0].type is FrameType.AUDIO
    assert frames[0].payload == pcm, "the payload was altered in transit"


@pytest.mark.parametrize("size", [320, 640, 3_840, 7_680, MAX_REASONABLE_AUDIO_BYTES])
def test_legitimate_payload_sizes_are_accepted(size: int) -> None:
    """320 is 20 ms at 8 kHz; 640 at 16 kHz; the rest are what real PBXs send."""
    decoder = FrameDecoder(max_payload=MAX_REASONABLE_AUDIO_BYTES)
    pcm = _pcm(size)

    frames = decoder.feed(audio_frame(pcm))

    assert len(frames) == 1
    assert frames[0].payload == pcm


def test_one_byte_over_the_limit_is_refused() -> None:
    """The bound is still a bound: DoS protection must survive the fix."""
    decoder = FrameDecoder(max_payload=MAX_REASONABLE_AUDIO_BYTES)
    oversized = MAX_REASONABLE_AUDIO_BYTES + 1
    header = bytes([FrameType.AUDIO]) + oversized.to_bytes(2, "big")

    with pytest.raises(ProtocolError, match="declares"):
        decoder.feed(header)


def test_the_limit_is_refused_before_the_payload_is_allocated() -> None:
    """A hostile length must not let a peer reserve memory by declaring it."""
    decoder = FrameDecoder(max_payload=MAX_REASONABLE_AUDIO_BYTES)
    header = bytes([FrameType.AUDIO]) + MAX_PAYLOAD_BYTES.to_bytes(2, "big")

    with pytest.raises(ProtocolError):
        decoder.feed(header)  # header only — no payload sent at all
    assert decoder.buffered_bytes <= HEADER_BYTES + MAX_REASONABLE_AUDIO_BYTES


def test_the_bound_stays_within_the_protocol_ceiling() -> None:
    assert MAX_REASONABLE_AUDIO_BYTES < MAX_PAYLOAD_BYTES
    assert MAX_REASONABLE_AUDIO_BYTES > OBSERVED_LIVE_PAYLOAD


# --- Stream framing ---------------------------------------------------------
def test_a_large_payload_split_across_reads_is_reassembled() -> None:
    """TCP gives a stream: a 7680-byte frame will not arrive in one read()."""
    decoder = FrameDecoder(max_payload=MAX_REASONABLE_AUDIO_BYTES)
    pcm = _pcm(OBSERVED_LIVE_PAYLOAD)
    wire = audio_frame(pcm)

    collected = []
    for start in range(0, len(wire), 4096):  # the real read size
        collected.extend(decoder.feed(wire[start : start + 4096]))

    assert len(collected) == 1
    assert collected[0].payload == pcm


def test_several_frames_in_one_read_all_emerge_in_order() -> None:
    decoder = FrameDecoder(max_payload=MAX_REASONABLE_AUDIO_BYTES)
    payloads = [_pcm(320), _pcm(7_680), _pcm(640)]

    frames = decoder.feed(b"".join(audio_frame(p) for p in payloads))

    assert [f.payload for f in frames] == payloads


def test_a_truncated_payload_yields_nothing_until_complete() -> None:
    decoder = FrameDecoder(max_payload=MAX_REASONABLE_AUDIO_BYTES)
    pcm = _pcm(OBSERVED_LIVE_PAYLOAD)
    wire = audio_frame(pcm)

    assert decoder.feed(wire[:-1]) == [], "an incomplete frame was emitted"
    frames = decoder.feed(wire[-1:])
    assert len(frames) == 1 and frames[0].payload == pcm


def test_a_peer_that_disconnects_mid_payload_leaves_no_frame() -> None:
    """Half a frame is not a frame; the buffer holds it and nothing is emitted."""
    decoder = FrameDecoder(max_payload=MAX_REASONABLE_AUDIO_BYTES)
    wire = audio_frame(_pcm(OBSERVED_LIVE_PAYLOAD))

    assert decoder.feed(wire[: HEADER_BYTES + 100]) == []
    assert decoder.buffered_bytes == HEADER_BYTES + 100


# --- Other frame types ------------------------------------------------------
def test_a_uuid_frame_of_the_wrong_length_is_rejected() -> None:
    with pytest.raises(ProtocolError, match="16 bytes"):
        parse_uuid(b"\x01\x02\x03")


def test_a_valid_uuid_frame_round_trips() -> None:
    call_id = uuid.uuid4()
    assert parse_uuid(call_id.bytes) == call_id


@pytest.mark.parametrize("payload", [b"", b"12", b"Z"])
def test_a_malformed_dtmf_frame_is_rejected(payload: bytes) -> None:
    with pytest.raises(ProtocolError):
        parse_dtmf(payload)


def test_an_unknown_frame_type_is_skipped_without_desyncing() -> None:
    """A newer Asterisk adding a type must not end the call."""
    decoder = FrameDecoder(max_payload=MAX_REASONABLE_AUDIO_BYTES)
    unknown = bytes([0x7E]) + (4).to_bytes(2, "big") + b"abcd"
    pcm = _pcm(320)

    frames = decoder.feed(unknown + audio_frame(pcm))

    assert [f.payload for f in frames] == [pcm], "the stream lost alignment"


# --- Reframing: wire packets in, 20 ms processing frames out ----------------
async def test_a_large_packet_becomes_whole_20ms_frames(pbx) -> None:
    """The pipeline works in 20 ms frames; the wire does not have to.

    One 7680-byte packet is 480 ms of 8 kHz slin, resampled to 16 kHz on the
    way in. It must arrive as whole 20 ms frames — never as one 480 ms frame,
    which is what the pipeline would have received had the transport simply
    forwarded the packet.

    20 frames rather than 24: the streaming resampler holds roughly 80 ms in
    its filter, which is retained state and not loss — the next packet carries
    it through. Resampling per packet instead would put a discontinuity at
    every boundary, which is why it is streaming in the first place.
    """
    channel = await pbx.connect()
    await channel.start()
    audio_in = AudioSocketInput(channel)

    received: list[bytes] = []

    async def consume() -> None:
        async for frame in audio_in.frames():
            received.append(frame)
            if len(received) >= 20:
                return

    await pbx.send_raw(audio_frame(_pcm(OBSERVED_LIVE_PAYLOAD)))
    await asyncio.wait_for(consume(), timeout=5.0)
    await channel.close()

    assert len(received) == 20
    assert all(len(f) == FRAME_BYTES for f in received), (
        "a packet was forwarded whole instead of being cut into 20 ms frames"
    )


async def test_audio_keeps_flowing_across_several_large_packets(pbx) -> None:
    """The resampler's held-back tail is carried forward, not dropped.

    Paced roughly at real time: delivering seconds of audio instantly trips the
    deliberate overrun policy, which drops stale frames on a live call. That
    behaviour is covered elsewhere; this is about continuity.
    """
    channel = await pbx.connect()
    await channel.start()
    audio_in = AudioSocketInput(channel)

    received: list[bytes] = []

    async def consume() -> None:
        async for frame in audio_in.frames():
            received.append(frame)
            if len(received) >= 44:
                return

    async def send() -> None:
        for _ in range(3):
            await pbx.send_raw(audio_frame(_pcm(OBSERVED_LIVE_PAYLOAD)))
            await asyncio.sleep(0.25)

    await asyncio.gather(send(), asyncio.wait_for(consume(), timeout=8.0))
    await channel.close()

    assert len(received) >= 44, "audio stopped flowing after the first packet"
    assert all(len(f) == FRAME_BYTES for f in received)


async def test_a_packet_that_is_not_a_whole_number_of_frames_keeps_the_remainder(
    pbx,
) -> None:
    """Packet boundaries and frame boundaries need not line up.

    500 bytes of 8 kHz slin is 31.25 ms — not a whole number of 20 ms frames.
    The remainder must be carried into the next packet rather than discarded,
    or samples vanish a few milliseconds at a time.
    """
    channel = await pbx.connect()
    await channel.start()
    audio_in = AudioSocketInput(channel)

    received: list[bytes] = []

    async def consume() -> None:
        async for frame in audio_in.frames():
            received.append(frame)
            if len(received) >= 8:
                return

    async def send() -> None:
        for _ in range(12):
            await pbx.send_raw(audio_frame(_pcm(500)))
            await asyncio.sleep(0.02)

    await asyncio.gather(send(), asyncio.wait_for(consume(), timeout=8.0))
    await channel.close()

    assert all(len(f) == FRAME_BYTES for f in received), "a partial frame was emitted"


async def test_an_odd_length_pcm16_payload_is_rejected_without_ending_the_call(pbx) -> None:
    """Half a sample is malformed, but it must not kill the media.

    Left to reach numpy it raises "buffer size must be a multiple of element
    size", which surfaced as an unexplained read failure rather than a bad
    frame.
    """
    channel = await pbx.connect()
    await channel.start()
    audio_in = AudioSocketInput(channel)

    received: list[bytes] = []

    async def consume() -> None:
        async for frame in audio_in.frames():
            received.append(frame)
            if len(received) >= 10:
                return

    async def send() -> None:
        await pbx.send_raw(audio_frame(_pcm(321)))  # odd: not a whole sample
        for _ in range(3):
            await pbx.send_raw(audio_frame(_pcm(OBSERVED_LIVE_PAYLOAD)))
            await asyncio.sleep(0.15)

    await asyncio.gather(send(), asyncio.wait_for(consume(), timeout=8.0))

    assert channel.is_running, "an odd-length frame ended the media"
    assert received, "good audio after a bad frame never arrived"
    await channel.close()
