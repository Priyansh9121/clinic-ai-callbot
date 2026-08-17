"""AudioSocket protocol and the telephony audio transport.

The media path is tested over **real loopback TCP sockets** speaking the real
wire protocol, not against a mock. Framing, codec conversion, pacing and
disconnect handling are where a telephony integration actually breaks, and a
mock would assert only that the code calls itself.
"""

from __future__ import annotations

import asyncio
import time

import numpy as np
import pytest

from app.speech import audio as audio_utils
from app.speech.providers import FRAME_BYTES, FRAME_MS
from app.telephony.audiosocket import (
    HEADER_BYTES,
    MAX_REASONABLE_AUDIO_BYTES,
    Frame,
    FrameDecoder,
    FrameType,
    ProtocolError,
    audio_frame,
    parse_dtmf,
    parse_uuid,
    terminate_frame,
    uuid_frame,
)
from app.telephony.transport import (
    AudioSocketInput,
    AudioSocketOutput,
    TelephonyFormat,
    _StreamResampler,
)
from tests.fixtures.fake_telephony import FakeAsterisk, wire_silence, wire_tone

BYTES_PER_MS_CANONICAL = 32  # 16 kHz, 16-bit, mono


# --- Wire protocol ----------------------------------------------------------
def test_a_frame_round_trips_through_the_decoder() -> None:
    decoder = FrameDecoder()
    frames = decoder.feed(audio_frame(b"\x01\x02\x03\x04"))

    assert len(frames) == 1
    assert frames[0].type is FrameType.AUDIO
    assert frames[0].payload == b"\x01\x02\x03\x04"


def test_frames_split_across_reads_are_reassembled() -> None:
    """TCP delivers a stream, not messages. Half a header is normal."""
    encoded = audio_frame(b"abcdefgh")
    decoder = FrameDecoder()

    assert decoder.feed(encoded[:2]) == []
    assert decoder.feed(encoded[2:5]) == []
    frames = decoder.feed(encoded[5:])

    assert len(frames) == 1
    assert frames[0].payload == b"abcdefgh"


def test_several_frames_in_one_read_are_all_returned() -> None:
    decoder = FrameDecoder()
    blob = audio_frame(b"aa") + audio_frame(b"bb") + terminate_frame()
    frames = decoder.feed(blob)

    assert [f.type for f in frames] == [FrameType.AUDIO, FrameType.AUDIO, FrameType.TERMINATE]


def test_an_oversized_declared_length_is_refused_before_allocating() -> None:
    """The length field is attacker-influenced. 64 KB claims must not allocate."""
    decoder = FrameDecoder(max_payload=MAX_REASONABLE_AUDIO_BYTES)
    hostile = bytes([FrameType.AUDIO]) + (0xFFFF).to_bytes(2, "big")

    with pytest.raises(ProtocolError, match="limit"):
        decoder.feed(hostile)


def test_a_partial_frame_never_grows_the_buffer_without_bound() -> None:
    decoder = FrameDecoder(max_payload=MAX_REASONABLE_AUDIO_BYTES)
    header = bytes([FrameType.AUDIO]) + (1000).to_bytes(2, "big")
    decoder.feed(header + b"\x00" * 500)

    assert decoder.buffered_bytes == HEADER_BYTES + 500


def test_an_unknown_frame_type_is_skipped_without_desynchronising() -> None:
    """A newer Asterisk adding a frame type must not end the call."""
    decoder = FrameDecoder()
    unknown = bytes([0x77]) + (2).to_bytes(2, "big") + b"xy"
    frames = decoder.feed(unknown + audio_frame(b"real"))

    assert len(frames) == 1
    assert frames[0].payload == b"real"


def test_uuid_and_dtmf_payloads_are_validated() -> None:
    identifier = parse_uuid(uuid_frame()[HEADER_BYTES:])
    assert identifier.version == 4
    assert parse_dtmf(b"5") == "5"

    with pytest.raises(ProtocolError):
        parse_uuid(b"too-short")
    with pytest.raises(ProtocolError):
        parse_dtmf(b"Z")  # not in the DTMF alphabet
    with pytest.raises(ProtocolError):
        parse_dtmf(b"12")


def test_a_payload_over_the_protocol_maximum_cannot_be_encoded() -> None:
    with pytest.raises(ValueError, match="exceeds"):
        Frame(FrameType.AUDIO, b"\x00" * 70_000).encode()


# --- Codec conversion -------------------------------------------------------
@pytest.mark.parametrize(
    ("wire", "expected_rate", "expected_frame"),
    [
        (TelephonyFormat.SLIN, 8_000, 320),
        (TelephonyFormat.SLIN16, 16_000, 640),
        (TelephonyFormat.PCMU, 8_000, 160),
        (TelephonyFormat.PCMA, 8_000, 160),
    ],
)
def test_each_wire_format_declares_its_own_frame_geometry(
    wire: TelephonyFormat, expected_rate: int, expected_frame: int
) -> None:
    assert wire.sample_rate == expected_rate
    assert wire.frame_bytes == expected_frame


@pytest.mark.parametrize("wire", [TelephonyFormat.PCMU, TelephonyFormat.PCMA])
def test_g711_survives_a_round_trip_at_speech_level(wire: TelephonyFormat) -> None:
    """G.711 is lossy but bounded: the spec is ~38 dB SNR for speech."""
    source = np.frombuffer(wire_tone(200, amplitude=12000), dtype="<i2").astype(np.float64)
    encoded = wire.from_pcm16(source.astype("<i2").tobytes())
    decoded = np.frombuffer(wire.to_pcm16(encoded), dtype="<i2").astype(np.float64)

    assert len(encoded) == len(source)  # 8-bit codewords, half the bytes
    snr = 10 * np.log10(np.sum(source**2) / np.sum((decoded - source) ** 2))
    assert snr > 30, f"{wire} round trip only achieved {snr:.1f} dB"


def test_the_resampler_holds_a_tail_that_flush_recovers() -> None:
    """Guards the bug where every reply lost its last 100 ms.

    soxr's streaming resampler keeps samples in its filter. Without an explicit
    flush at the end of an utterance they stay there until the *next* reply.
    """
    resampler = _StreamResampler(16_000, 8_000, quality="HQ")
    one_second = b"\x01\x02" * 8_000

    converted = resampler.convert(one_second)
    tail = resampler.flush()

    assert len(tail) > 0, "nothing held back — the test no longer proves anything"
    total_samples = (len(converted) + len(tail)) // 2
    assert total_samples == 4_000, "flush must recover exactly the missing tail"


def test_resetting_the_resampler_discards_an_abandoned_utterance() -> None:
    resampler = _StreamResampler(16_000, 8_000, quality="HQ")
    resampler.convert(b"\x00\x40" * 8_000)  # loud audio, now inside the filter
    resampler.reset()

    assert resampler.flush() == b""


# --- Transport over a real socket -------------------------------------------
@pytest.fixture
async def pbx():
    fake = FakeAsterisk()
    await fake.start()
    yield fake
    await fake.stop()


async def test_inbound_telephone_audio_becomes_canonical_frames(pbx: FakeAsterisk) -> None:
    """8 kHz on the wire, 16 kHz canonical frames to the voice pipeline."""
    channel = await pbx.connect()
    audio_in = AudioSocketInput(channel)
    await pbx.send_audio(wire_tone(400))

    received: list[bytes] = []

    async def consume() -> None:
        async for frame in audio_in.frames():
            received.append(frame)
            if len(received) >= 10:
                return

    await asyncio.wait_for(consume(), timeout=3.0)
    await channel.close()

    assert all(len(frame) == FRAME_BYTES for frame in received)
    assert audio_utils.rms(b"".join(received)) > 0.01


async def test_the_media_channel_learns_the_call_uuid(pbx: FakeAsterisk) -> None:
    """Correlation between a socket and a call depends on this."""
    channel = await pbx.connect()
    found = await channel.wait_for_uuid(limit_seconds=2.0)
    await channel.close()

    assert found == str(pbx.call_uuid)


async def test_outbound_audio_is_paced_at_real_time(pbx: FakeAsterisk) -> None:
    """Not a quality nicety: pacing is what bounds un-recallable audio."""
    channel = await pbx.connect()
    audio_out = AudioSocketOutput(channel)

    started = time.monotonic()
    await audio_out.play(b"\x01\x02" * (500 * BYTES_PER_MS_CANONICAL // 2))
    queued_after = time.monotonic() - started
    await asyncio.wait_for(audio_out.wait_until_drained(), timeout=5.0)
    elapsed = time.monotonic() - started
    await channel.close()

    assert queued_after < 0.1, "play() must queue, not block for the audio's duration"
    assert 0.45 < elapsed < 0.85, f"500 ms of audio took {elapsed:.2f} s to transmit"


async def test_no_audio_is_lost_between_the_pipeline_and_the_wire(pbx: FakeAsterisk) -> None:
    channel = await pbx.connect()
    audio_out = AudioSocketOutput(channel)

    await audio_out.play(b"\x01\x02" * (300 * BYTES_PER_MS_CANONICAL // 2))
    await asyncio.wait_for(audio_out.wait_until_drained(), timeout=5.0)
    await asyncio.sleep(0.05)
    await channel.close()

    assert abs(pbx.received_ms - 300) <= 21, f"expected ~300 ms, Asterisk saw {pbx.received_ms:.0f}"


async def test_every_transmitted_frame_is_exactly_twenty_milliseconds(pbx: FakeAsterisk) -> None:
    channel = await pbx.connect()
    audio_out = AudioSocketOutput(channel)

    await audio_out.play(b"\x01\x02" * (200 * BYTES_PER_MS_CANONICAL // 2))
    await asyncio.wait_for(audio_out.wait_until_drained(), timeout=5.0)
    await asyncio.sleep(0.05)
    await channel.close()

    assert pbx.frame_sizes
    assert set(pbx.frame_sizes) == {TelephonyFormat.SLIN.frame_bytes}


async def test_barge_in_discards_queued_audio_immediately(pbx: FakeAsterisk) -> None:
    channel = await pbx.connect()
    audio_out = AudioSocketOutput(channel)

    await audio_out.play(b"\x01\x02" * (2000 * BYTES_PER_MS_CANONICAL // 2))
    await asyncio.sleep(0.08)

    started = time.monotonic()
    await audio_out.stop_immediately()
    stop_ms = (time.monotonic() - started) * 1000
    transmitted_at_stop = pbx.received_ms
    await asyncio.sleep(0.15)
    await channel.close()

    assert stop_ms < 50, f"stop_immediately took {stop_ms:.1f} ms"
    assert audio_out.discarded_frames > 0
    # Nothing further may reach the caller once playback is abandoned.
    assert pbx.received_ms - transmitted_at_stop <= 40


async def test_pacing_bounds_how_much_audio_cannot_be_recalled(pbx: FakeAsterisk) -> None:
    """The barge-in guarantee: a two-second reply is not already in Asterisk."""
    channel = await pbx.connect()
    audio_out = AudioSocketOutput(channel)

    await audio_out.play(b"\x01\x02" * (2000 * BYTES_PER_MS_CANONICAL // 2))
    await asyncio.sleep(0.1)
    await audio_out.stop_immediately()
    await asyncio.sleep(0.1)
    await channel.close()

    assert pbx.received_ms < 300, (
        f"{pbx.received_ms:.0f} ms had already been transmitted after 100 ms — "
        "pacing is not limiting what the caller will still hear"
    )


async def test_a_dtmf_frame_is_delivered_as_structured_input(pbx: FakeAsterisk) -> None:
    """Keypad digits must not be routed through speech recognition."""
    channel = await pbx.connect()
    digits: list[str] = []
    channel.on_dtmf(digits.append)

    for digit in "1234":
        await pbx.send_dtmf(digit)
    await asyncio.sleep(0.1)
    await channel.close()

    assert digits == ["1", "2", "3", "4"]


async def test_a_terminate_frame_ends_the_media_stream(pbx: FakeAsterisk) -> None:
    channel = await pbx.connect()
    hung_up = asyncio.Event()
    channel.on_hangup(hung_up.set)

    await pbx.send_raw(terminate_frame())
    await asyncio.wait_for(hung_up.wait(), timeout=2.0)
    await channel.close()

    assert not channel.is_running


async def test_the_caller_hanging_up_closes_the_media_cleanly(pbx: FakeAsterisk) -> None:
    channel = await pbx.connect()
    hung_up = asyncio.Event()
    channel.on_hangup(hung_up.set)

    await pbx.hang_up()
    await asyncio.wait_for(hung_up.wait(), timeout=2.0)

    assert not channel.is_running
    await channel.close()


async def test_a_malformed_frame_ends_media_without_crashing(pbx: FakeAsterisk) -> None:
    channel = await pbx.connect()
    hung_up = asyncio.Event()
    channel.on_hangup(hung_up.set)

    await pbx.send_raw(bytes([FrameType.AUDIO]) + (0xFFFF).to_bytes(2, "big") + b"\x00" * 10)
    await asyncio.wait_for(hung_up.wait(), timeout=2.0)
    await channel.close()

    assert not channel.is_running


async def test_a_flood_of_inbound_audio_does_not_grow_without_bound(pbx: FakeAsterisk) -> None:
    """A fast producer with no consumer must drop, never accumulate."""
    channel = await pbx.connect()

    await pbx.send_audio(wire_tone(4000))  # 4 s, nothing reading it
    await asyncio.sleep(0.3)
    depth = channel._inbound.qsize()
    await channel.close()

    assert depth <= 50, f"inbound queue reached {depth} frames"
    assert channel.overruns > 0, "expected overruns to be counted, not hidden"


async def test_writing_after_the_socket_dies_does_not_raise(pbx: FakeAsterisk) -> None:
    """A caller who hangs up mid-reply must not produce an unhandled error."""
    channel = await pbx.connect()
    audio_out = AudioSocketOutput(channel)

    await pbx.hang_up()
    await asyncio.sleep(0.1)
    await audio_out.play(b"\x01\x02" * 8000)
    await asyncio.sleep(0.1)
    await channel.close()


async def test_silence_from_the_wire_produces_silent_canonical_frames(pbx: FakeAsterisk) -> None:
    channel = await pbx.connect()
    audio_in = AudioSocketInput(channel)
    await pbx.send_audio(wire_silence(200))

    received: list[bytes] = []

    async def consume() -> None:
        async for frame in audio_in.frames():
            received.append(frame)
            if len(received) >= 4:
                return

    await asyncio.wait_for(consume(), timeout=3.0)
    await channel.close()

    assert audio_utils.rms(b"".join(received)) < 0.001


# --- The transport's own share of perceived latency -------------------------
async def test_the_transport_measures_first_audio_to_first_byte(pbx: FakeAsterisk) -> None:
    """Everything above the socket can measure a reply; none of it can time one.

    VoiceSession knows when synthesis produced its first chunk. What that chunk
    then costs — the resampler holding audio back, the outbound queue, the
    pacer waiting for its slot — is only visible here, and on a real call it is
    part of what the caller waits through.
    """
    channel = await pbx.connect()
    await channel.start()
    try:
        assert channel.last_reply_wire_latency_ms is None, "nothing sent yet"

        audio_out = AudioSocketOutput(channel)
        await audio_out.play(audio_utils.silence(200))
        await audio_out.wait_until_drained()

        measured = channel.last_reply_wire_latency_ms
        assert measured is not None, "a reply was sent and never timed"
        assert measured >= 0.0
        # DELIBERATELY LOOSE. This asserts the measurement happens and is not
        # nonsense; it is not a regression bound. Loopback with no real
        # resampling is nothing like a call, so a tight number here would
        # describe the test rather than the transport. Pin this once a live
        # call has produced a real figure — see docs/architecture/latency.md.
        assert measured < 1_000, f"implausible wire latency {measured} ms"
    finally:
        await channel.close()


async def test_each_reply_is_timed_separately(pbx: FakeAsterisk) -> None:
    """The clock starts when an idle transport receives audio, not once per call.

    Without the idle check every frame of a long reply would restart it, and
    the number would describe the pacer's 20 ms tick rather than the wait
    before the caller hears anything.
    """
    channel = await pbx.connect()
    await channel.start()
    try:
        audio_out = AudioSocketOutput(channel)
        await audio_out.play(audio_utils.silence(200))
        await audio_out.wait_until_drained()
        first = channel.last_reply_wire_latency_ms

        await audio_out.play(audio_utils.silence(200))
        await audio_out.wait_until_drained()
        second = channel.last_reply_wire_latency_ms

        assert first is not None and second is not None
        # Both are measurements of their own reply rather than a running total.
        # Bound is a smoke check, not a regression bound — see the note above.
        assert second < 1_000
    finally:
        await channel.close()


async def test_the_transport_records_inbound_arrival_and_dwell(pbx: FakeAsterisk) -> None:
    """endpoint_latency_ms is timed from when the VAD saw silence.

    That is dwell_ms after the caller actually stopped talking, and if frames
    arrive batched rather than every 20 ms it is later still. Without these two
    readings a slow endpointer and a bursty transport look identical from
    above, which is the confusion they exist to prevent.

    Bounds are deliberately loose for the same reason as the outbound tests:
    the fake delivers audio far faster than a phone line.
    """
    channel = await pbx.connect()
    await channel.start()
    try:
        assert channel.inbound_arrival_mean_ms is None, "nothing received yet"
        assert channel.inbound_dwell_mean_ms is None

        audio_in = AudioSocketInput(channel)
        received: list[bytes] = []

        async def consume() -> None:
            async for frame in audio_in.frames():
                received.append(frame)
                if len(received) >= 5:
                    return

        await pbx.hold_line()
        await asyncio.wait_for(consume(), timeout=5.0)

        assert channel.inbound_arrival_mean_ms is not None, "arrivals were never timed"
        assert channel.inbound_dwell_mean_ms is not None, "queue dwell was never timed"
        assert channel.inbound_arrival_max_ms >= channel.inbound_arrival_mean_ms
        assert channel.inbound_dwell_max_ms >= channel.inbound_dwell_mean_ms
    finally:
        await channel.close()


async def test_a_reply_longer_than_the_queue_arrives_complete(pbx: FakeAsterisk) -> None:
    """The greeting was being truncated on every call, including the good ones.

    Piper synthesises at roughly 30x real time; the pacer drains one 20 ms
    frame every 20 ms. Any reply longer than the queue holds therefore overran
    it, and the old policy dropped the overflow — which is why every live call
    logged telephony.outbound_full three times during the greeting.

    A bigger queue only moves the threshold, because the rates differ by 30x.
    Backpressure holds at any length.
    """
    channel = await pbx.connect()
    await channel.start()
    try:
        audio_out = AudioSocketOutput(channel)
        # Comfortably more than DEFAULT_OUTBOUND_FRAMES (250 frames = 5 s).
        spoken_ms = 8_000
        await audio_out.play(audio_utils.silence(spoken_ms))
        await asyncio.wait_for(audio_out.wait_until_drained(), timeout=30.0)
        await asyncio.sleep(0.1)

        expected = spoken_ms / FRAME_MS
        # Every frame synthesised reached the wire. Allow a frame either side
        # for the resampler's tail landing on a boundary.
        assert pbx.received_ms >= spoken_ms - FRAME_MS, (
            f"reply truncated: sent {spoken_ms} ms, wire saw {pbx.received_ms} ms"
        )
        assert channel.outbound_frames_sent >= expected - 1
    finally:
        await channel.close()


async def test_barge_in_still_abandons_a_reply_held_by_backpressure(
    pbx: FakeAsterisk,
) -> None:
    """Backpressure must not turn a superseded reply into queued audio.

    A synthesiser parked on a full queue wakes when the pacer drains. If it
    simply carried on, the abandoned reply would be queued behind the new
    turn's audio and the caller would hear the answer they interrupted.
    """
    channel = await pbx.connect()
    await channel.start()
    try:
        audio_out = AudioSocketOutput(channel)
        long_reply = asyncio.create_task(audio_out.play(audio_utils.silence(8_000)))
        await asyncio.sleep(0.2)  # let it fill the queue and park

        await audio_out.stop_immediately()
        await asyncio.wait_for(long_reply, timeout=10.0)
        await asyncio.sleep(0.1)

        # Whatever the pacer already wrote is gone for good; nothing more of
        # the abandoned reply may be waiting behind it.
        assert channel.pending_outbound_frames == 0, "the abandoned reply was queued after barge-in"
    finally:
        await channel.close()
