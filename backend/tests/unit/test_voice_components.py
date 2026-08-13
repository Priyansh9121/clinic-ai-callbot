"""Audio contract, VAD, endpointing, turn tokens and speech normalisation.

All offline — no models, no sound card.
"""

from __future__ import annotations

import pytest

from app.speech import audio
from app.speech.providers import FRAME_BYTES, SpeechEvent
from app.speech.vad.energy import EnergyVAD
from app.voice.endpointing import EndpointConfig, Endpointer, EndpointReason
from app.voice.speech_text import for_speech, practitioner_for_speech
from app.voice.states import (
    INTERRUPTIBLE_STATES,
    StaleTurnError,
    TurnTracker,
    VoiceState,
)


def loud_frame(ms: int = 20) -> bytes:
    return b"\x30\x30" * (audio.CANONICAL.bytes_per_second * ms // 1000 // 2)


# --- Audio format contract --------------------------------------------------
def test_canonical_format_is_16k_mono_16bit() -> None:
    assert audio.CANONICAL.sample_rate_hz == 16_000
    assert audio.CANONICAL.channels == 1
    assert audio.CANONICAL.sample_width_bytes == 2
    assert FRAME_BYTES == 640  # 20 ms


def test_silence_and_duration_round_trip() -> None:
    pcm = audio.silence(100)
    assert len(pcm) == 3200
    assert audio.duration_ms(pcm) == pytest.approx(100.0)
    assert audio.rms(pcm) == 0.0
    assert audio.dbfs(pcm) == -100.0


@pytest.mark.parametrize(("from_rate", "to_rate"), [(48000, 16000), (8000, 16000), (22050, 16000)])
def test_resampling_preserves_duration(from_rate: int, to_rate: int) -> None:
    """Telephony (8 kHz) and Piper (22.05 kHz) both go through this path."""
    one_second = b"\x00\x10" * from_rate
    resampled = audio.resample_pcm16(one_second, from_rate, to_rate)
    assert len(resampled) == pytest.approx(to_rate * 2, rel=0.02)


def test_resampling_is_a_no_op_at_the_same_rate() -> None:
    pcm = b"\x01\x02" * 100
    assert audio.resample_pcm16(pcm, 16000, 16000) == pcm


def test_stereo_is_downmixed() -> None:
    stereo = b"\x00\x10\x00\x10" * 1000
    mono = audio.resample_pcm16(stereo, 16000, 16000, channels=2)
    assert len(mono) == len(stereo) // 2


def test_iter_frames_discards_a_partial_tail() -> None:
    """VAD models need exact frame sizes; a short frame would be a false negative."""
    frames = audio.iter_frames(audio.silence(100) + b"\x00" * 10)
    assert len(frames) == 5
    assert all(len(f) == FRAME_BYTES for f in frames)


def test_float_conversion_round_trips() -> None:
    original = b"\x00\x40" * 100
    restored = audio.from_float32(audio.to_float32(original))
    assert len(restored) == len(original)


def test_from_float32_clips_rather_than_wrapping() -> None:
    """A value over 1.0 must not wrap to a large negative sample (a click)."""
    import numpy as np

    pcm = audio.from_float32(np.array([2.0, -2.0], dtype=np.float32))
    samples = audio.to_int16_array(pcm)
    assert samples[0] > 32000 and samples[1] < -32000


# --- Energy VAD -------------------------------------------------------------
def test_energy_vad_detects_speech_start_and_end() -> None:
    vad = EnergyVAD(start_frames=1, stop_frames=2)
    assert vad.process_frame(audio.silence(20)) is None
    assert vad.process_frame(loud_frame()) is SpeechEvent.SPEECH_STARTED
    assert vad.is_speaking is True
    assert vad.process_frame(audio.silence(20)) is None  # hangover
    assert vad.process_frame(audio.silence(20)) is SpeechEvent.SPEECH_ENDED
    assert vad.is_speaking is False


def test_energy_vad_emits_only_on_transitions() -> None:
    vad = EnergyVAD(start_frames=1, stop_frames=2)
    vad.process_frame(loud_frame())
    assert all(vad.process_frame(loud_frame()) is None for _ in range(5))


def test_energy_vad_hysteresis_requires_stop_below_start() -> None:
    with pytest.raises(ValueError, match="hysteresis"):
        EnergyVAD(start_dbfs=-50.0, stop_dbfs=-40.0)


def test_energy_vad_reset_clears_state() -> None:
    vad = EnergyVAD(start_frames=1)
    vad.process_frame(loud_frame())
    vad.reset()
    assert vad.is_speaking is False


# --- Endpointing ------------------------------------------------------------
def feed(endpointer: Endpointer, *, speech: int, silence_frames: int):
    """Drive the endpointer, returning the first endpoint (or None)."""
    for _ in range(speech):
        result = endpointer.process(SpeechEvent.SPEECH_STARTED, is_speech=True)
        if result:
            return result
    for _ in range(silence_frames):
        result = endpointer.process(None, is_speech=False)
        if result:
            return result
    return None


def test_endpoint_fires_after_configured_silence() -> None:
    endpointer = Endpointer(EndpointConfig(end_silence_ms=200, min_speech_ms=40))
    result = feed(endpointer, speech=10, silence_frames=10)
    assert result is not None
    assert result.reason is EndpointReason.END_SILENCE
    assert result.is_usable is True


def test_short_bursts_are_not_turns() -> None:
    """A cough or a door must not become an empty caller turn."""
    endpointer = Endpointer(EndpointConfig(end_silence_ms=100, min_speech_ms=300))
    result = feed(endpointer, speech=2, silence_frames=10)
    assert result is not None
    assert result.reason is EndpointReason.TOO_SHORT
    assert result.is_usable is False


def test_silence_with_no_speech_reports_no_speech() -> None:
    endpointer = Endpointer(EndpointConfig(initial_silence_ms=100))
    result = None
    for _ in range(20):
        result = endpointer.process(None, is_speech=False)
        if result:
            break
    assert result is not None
    assert result.reason is EndpointReason.NO_SPEECH
    assert result.is_usable is False


def test_max_duration_caps_a_caller_who_never_stops() -> None:
    endpointer = Endpointer(EndpointConfig(max_utterance_ms=200))
    result = None
    for _ in range(50):
        result = endpointer.process(SpeechEvent.SPEECH_STARTED, is_speech=True)
        if result:
            break
    assert result is not None
    assert result.reason is EndpointReason.MAX_DURATION
    # Still usable: the caller said something, it was just long.
    assert result.is_usable is True


def test_internal_pauses_do_not_end_the_turn() -> None:
    """ "I'd like an appointment... umm... on Friday" is one turn."""
    endpointer = Endpointer(EndpointConfig(end_silence_ms=300, min_speech_ms=40))
    for _ in range(10):
        assert endpointer.process(SpeechEvent.SPEECH_STARTED, is_speech=True) is None
    for _ in range(10):  # 200 ms pause, under the threshold
        assert endpointer.process(None, is_speech=False) is None
    for _ in range(10):
        assert endpointer.process(SpeechEvent.SPEECH_STARTED, is_speech=True) is None
    assert endpointer.speech_ms == pytest.approx(400.0)


@pytest.mark.parametrize("end_silence_ms", [300, 500, 700])
def test_endpoint_threshold_is_configurable(end_silence_ms: int) -> None:
    """The benchmark sweeps these values; the logic must honour each."""
    endpointer = Endpointer(EndpointConfig(end_silence_ms=end_silence_ms, min_speech_ms=40))
    for _ in range(10):
        endpointer.process(SpeechEvent.SPEECH_STARTED, is_speech=True)
    frames_needed = end_silence_ms // 20
    for _ in range(frames_needed - 1):
        assert endpointer.process(None, is_speech=False) is None
    assert endpointer.process(None, is_speech=False) is not None


def test_endpointer_reset_allows_reuse() -> None:
    endpointer = Endpointer(EndpointConfig(end_silence_ms=100, min_speech_ms=40))
    assert feed(endpointer, speech=10, silence_frames=10) is not None
    endpointer.reset()
    assert endpointer.speech_started is False
    assert feed(endpointer, speech=10, silence_frames=10) is not None


# --- Turn tokens ------------------------------------------------------------
def test_a_new_turn_supersedes_the_previous_one() -> None:
    tracker = TurnTracker()
    first = tracker.begin()
    second = tracker.begin()
    assert first.cancelled is True
    assert tracker.is_active(first) is False
    assert tracker.is_active(second) is True


def test_ensure_active_raises_for_a_superseded_turn() -> None:
    tracker = TurnTracker()
    first = tracker.begin()
    tracker.begin()
    with pytest.raises(StaleTurnError):
        tracker.ensure_active(first)


def test_cancellation_is_one_way() -> None:
    tracker = TurnTracker()
    token = tracker.begin()
    tracker.cancel_current()
    assert tracker.is_active(token) is False


def test_turn_ids_increase() -> None:
    tracker = TurnTracker()
    assert [tracker.begin().id for _ in range(3)] == [1, 2, 3]


def test_trackers_are_independent_between_sessions() -> None:
    """Two callers must not be able to cancel each other's turns."""
    a, b = TurnTracker(), TurnTracker()
    token_a = a.begin()
    b.begin()
    b.begin()
    assert a.is_active(token_a) is True


def test_interruptible_states_cover_all_agent_speech() -> None:
    assert VoiceState.SPEAKING in INTERRUPTIBLE_STATES
    # THINKING counts because an acknowledgement may still be playing.
    assert VoiceState.THINKING in INTERRUPTIBLE_STATES
    assert VoiceState.LISTENING not in INTERRUPTIBLE_STATES


# --- Speech normalisation ---------------------------------------------------
@pytest.mark.parametrize(
    ("text", "expected_fragment"),
    [
        ("call us on 13 74 25", "one three, seven four, two five"),
        ("mobile 0491 570 156", "zero four nine one, five seven zero, one five six"),
        ("Call 03 9123 4567.", "zero three, nine one two three, four five six seven"),
        ("Dr Sarah Patel", "Doctor Sarah Patel"),
    ],
)
def test_normalisation_dictates_numbers_and_expands_titles(
    text: str, expected_fragment: str
) -> None:
    assert expected_fragment in for_speech(text)


@pytest.mark.parametrize(
    "text",
    [
        "I have Friday at 12:15 pm, 3 pm or 4:15 pm. Which suits?",
        "A standard consultation is 85 dollars, rebate about 42 dollars.",
        "We are at 12 Riverside Drive, Richmond VIC 3121.",
        "We're open weekdays 8am to 6pm, and Saturdays 9am to 1pm.",
        "Please give at least four hours notice, otherwise a 30 dollar fee may apply.",
    ],
)
def test_normalisation_leaves_correct_text_alone(text: str) -> None:
    """Times, money and street numbers already pronounce correctly — measured
    by round-tripping the catalogue through Piper and Whisper — so rewriting
    them would add risk for no gain."""
    assert for_speech(text) == text


def test_normalisation_is_idempotent() -> None:
    once = for_speech("Call 0491 570 156 for Dr Patel.")
    assert for_speech(once) == once


def test_normalisation_handles_empty_input() -> None:
    assert for_speech("") == ""


def test_practitioner_names_are_spoken_without_changing_stored_values() -> None:
    assert practitioner_for_speech("Dr Emily Chen") == "Doctor Emily Chen"
