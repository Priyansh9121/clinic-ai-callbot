"""Checks that need the real speech models on disk.

    uv run pytest -m voice_live

Skipped by default: ``uv run pytest`` must pass on a machine with no Whisper
weights, no Piper voice and no microphone, so nothing in CI depends on them.

These verify what a test double cannot — that the real engines honour the audio
contract, that the safety phrases actually survive real transcription, and that
the cache serves the acknowledgement without synthesising it. Accuracy *ranking*
between models belongs in ``scripts/benchmark_stt.py``; these are regression
guards with generous margins, not a benchmark.

No audio is written to disk and no transcript is stored, exactly as in
production.
"""

from __future__ import annotations

import time

import pytest

from app.core.config import get_settings
from app.speech.audio import duration_ms, iter_frames, rms, silence
from app.speech.factory import build_stt, build_tts, build_vad
from app.speech.providers import FRAME_BYTES, SAMPLE_RATE_HZ, SpeechEvent
from app.speech.tts.cache import CachingTTS, cacheable_phrases
from app.voice.session import ACKNOWLEDGEMENT

pytestmark = pytest.mark.voice_live


@pytest.fixture(scope="module")
def settings():
    return get_settings()


@pytest.fixture(scope="module")
def stt(settings):
    provider = build_stt(settings)
    if provider is None:
        pytest.skip("STT_PROVIDER is not configured")
    return provider


@pytest.fixture(scope="module")
def tts(settings):
    provider = build_tts(settings)
    if provider is None:
        pytest.skip("TTS_PROVIDER is not configured")
    return provider


@pytest.fixture(scope="module")
def vad(settings):
    provider = build_vad(settings)
    if provider is None:
        pytest.skip("VAD_PROVIDER is not configured")
    return provider


def _normalise(text: str) -> str:
    return "".join(c for c in text.lower() if c.isalnum() or c.isspace()).strip()


# --- The audio contract, with real engines ----------------------------------
async def test_piper_output_matches_the_canonical_audio_format(tts) -> None:
    """22.05 kHz voice, 16 kHz pipeline. The resample happens in the provider."""
    audio = await tts.synthesize("Are you a new or existing patient?")

    assert len(audio) % 2 == 0, "16-bit samples must not be split"
    assert len(audio) % FRAME_BYTES == 0 or len(audio) > FRAME_BYTES
    assert duration_ms(audio) > 500
    assert rms(audio) > 0.01, "silent output means synthesis failed"


async def test_real_speech_survives_a_round_trip(tts, stt) -> None:
    """Piper out, Whisper back. The pipeline's two ends agree on the format."""
    audio = await tts.synthesize("I would like to book an appointment please")
    transcript = await stt.transcribe(audio)

    assert "appointment" in _normalise(transcript.text)
    assert transcript.is_final is True


async def test_silence_does_not_produce_a_caller_turn(stt) -> None:
    """Whisper hallucinates on silence; the provider must not pass it on."""
    transcript = await stt.transcribe(silence(1500))

    from app.speech.stt.whisper import looks_like_silence_hallucination

    text = transcript.text.strip()
    assert text == "" or looks_like_silence_hallucination(text, silence(1500))


async def test_an_oversized_utterance_is_bounded_not_refused(stt) -> None:
    """A stuck line must not become an unbounded transcription job."""
    from app.speech.stt.whisper import MAX_UTTERANCE_SECONDS

    started = time.perf_counter()
    transcript = await stt.transcribe(silence(int((MAX_UTTERANCE_SECONDS + 15) * 1000)))
    elapsed = time.perf_counter() - started

    assert transcript.is_final is True
    # Truncated to the cap, so the cost is bounded by MAX_UTTERANCE_SECONDS
    # rather than by how long the caller held the line open.
    assert elapsed < MAX_UTTERANCE_SECONDS


# --- Safety, through the real recogniser ------------------------------------
@pytest.mark.parametrize(
    ("spoken", "must_survive"),
    [
        ("I'm having chest pain", "chest pain"),
        ("I can't breathe properly", "breathe"),
        ("I think I'm having a heart attack", "heart attack"),
        ("he's unconscious and not responding", "unconscious"),
        ("I'm bleeding heavily and it won't stop", "bleeding"),
    ],
)
async def test_emergency_wording_survives_real_transcription(
    tts, stt, spoken: str, must_survive: str
) -> None:
    """If STT drops these, the deterministic SafetyEngine never sees them.

    This is the residual risk documented in docs/security/threat-model.md, held
    to account on the phrases that matter most. Synthetic speech is cleaner than
    a real caller, so passing here is necessary, not sufficient.
    """
    audio = await tts.synthesize(spoken)
    heard = _normalise((await stt.transcribe(audio)).text)

    assert must_survive in heard, f"lost {must_survive!r}: heard {heard!r}"


async def test_a_transcribed_emergency_still_routes_to_triple_zero(tts, stt) -> None:
    """End to end through the real engines: audio in, safety decision out."""
    from app.safety.engine import SafetyEngine

    audio = await tts.synthesize("I'm having chest pain and my arm hurts")
    transcript = await stt.transcribe(audio)

    verdict = SafetyEngine().evaluate(transcript.text)
    assert verdict.is_emergency is True


# --- VAD on real speech -----------------------------------------------------
async def test_silero_detects_real_speech_and_not_silence(tts, vad) -> None:
    """Guards the 64-sample context bug: without it, probabilities collapse."""
    vad.reset()
    speech = await tts.synthesize("Hello, I would like to make an appointment")

    started = False
    for frame in iter_frames(speech):
        if vad.process_frame(frame) is SpeechEvent.SPEECH_STARTED:
            started = True
            break
    assert started, "no speech detected in real synthesised speech"

    vad.reset()
    assert all(
        vad.process_frame(frame) is not SpeechEvent.SPEECH_STARTED
        for frame in iter_frames(silence(1000))
    )


# --- Cache behaviour with the real engine -----------------------------------
async def test_the_acknowledgement_is_served_from_cache(tts) -> None:
    """The phrase that hides LLM latency must not itself cost synthesis time."""
    cached = CachingTTS(tts, allowlist=cacheable_phrases())
    assert cached.is_cacheable(ACKNOWLEDGEMENT), "the acknowledgement must be cacheable"

    await cached.synthesize(ACKNOWLEDGEMENT)  # cold
    started = time.perf_counter()
    audio = await cached.synthesize(ACKNOWLEDGEMENT)  # warm
    warm_ms = (time.perf_counter() - started) * 1000

    assert len(audio) > 0
    assert warm_ms < 5, f"cache hit took {warm_ms:.2f} ms"


async def test_patient_specific_replies_are_never_cached(tts) -> None:
    """A cache of filled templates would be a store of patient data."""
    cached = CachingTTS(tts, allowlist=cacheable_phrases())
    reply = "Booked. Dr Sarah Patel, tomorrow at 12 pm. Anything else?"

    assert cached.is_cacheable(reply) is False
    before = cached.size
    await cached.synthesize(reply)
    assert cached.size == before


# --- Latency floors ---------------------------------------------------------
@pytest.mark.parametrize("word", ["Hughes", "yes", "Thursday"])
async def test_single_word_answers_are_not_slow(tts, stt, word: str) -> None:
    """A surname or a "yes" is the most common turn in a clinic call.

    Guards the temperature-fallback trap: with faster-whisper's default retry
    loop these cost ~1.6 s rather than ~0.4 s, for identical text. If this
    regresses, check that `temperature=0.0` is still pinned in the provider.
    """
    audio = await tts.synthesize(word)

    started = time.perf_counter()
    await stt.transcribe(audio)
    elapsed_ms = (time.perf_counter() - started) * 1000

    assert elapsed_ms < 900, f"{word!r} took {elapsed_ms:.0f} ms"


async def test_transcription_is_faster_than_real_time(tts, stt) -> None:
    """A real-time factor above 1.0 means the agent falls further behind
    with every turn. Generous bound — the benchmark measures ~0.36."""
    audio = await tts.synthesize(
        "I was hoping to see Doctor Patel sometime on Friday afternoon if that suits"
    )
    started = time.perf_counter()
    await stt.transcribe(audio)
    elapsed_ms = (time.perf_counter() - started) * 1000

    assert elapsed_ms / duration_ms(audio) < 0.8


async def test_first_audio_arrives_promptly(tts) -> None:
    """Streaming must yield the first sentence, not wait for the whole reply."""
    started = time.perf_counter()
    async for _ in tts.synthesize_stream(
        "I have Friday at 8 am, 9 am or 11:45 am. Which of those suits you best?"
    ):
        first_ms = (time.perf_counter() - started) * 1000
        break

    assert first_ms < 500, f"first audio took {first_ms:.0f} ms"


def test_the_pipeline_sample_rate_is_the_declared_one() -> None:
    assert SAMPLE_RATE_HZ == 16_000
    assert FRAME_BYTES == 640
