#!/usr/bin/env python
"""Measure local TTS latency and intelligibility on clinic replies.

    uv run python scripts/benchmark_tts.py
    uv run python scripts/benchmark_tts.py --voices en_GB-alba-medium,en_US-lessac-medium
    uv run python scripts/benchmark_tts.py --show

Latency is easy to measure and easy to over-value. A voice that starts in 40 ms
and says "quarter to eleven" as "1145" is worse than one that starts in 120 ms
and is understood, so this reports both:

    time to first audio   what the caller waits before hearing anything
    intelligibility       the reply spoken, transcribed back, and compared

The intelligibility loop is a *proxy*, not a listening test: it measures what
Whisper recovers, and Whisper is not a patient. It catches the failures that
actually bite — mangled times, dates, phone numbers and practitioner names —
which are exactly the parts of a clinic reply a caller must get right.

The phrases come from the real response catalogue (``app.conversation.responses``)
so this measures what the agent actually says. Templates are filled with
synthetic values; no patient data is involved anywhere.
"""

from __future__ import annotations

import argparse
import asyncio
import statistics
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend"))

from app.core.config import get_settings  # noqa: E402
from app.core.logging import configure_logging  # noqa: E402
from app.speech import audio as audio_utils  # noqa: E402
from app.speech.factory import build_stt  # noqa: E402
from app.speech.tts.piper import PiperTTS  # noqa: E402
from app.voice.speech_text import for_speech  # noqa: E402

sys.path.insert(0, str(REPO_ROOT / "scripts"))
from benchmark_stt import normalise, word_error_rate  # noqa: E402

BOLD, DIM, GREEN, YELLOW, RED, RESET = (
    "\033[1m", "\033[2m", "\033[32m", "\033[33m", "\033[31m", "\033[0m"
)

#: Replies whose details the caller must get right the first time. Wrong time,
#: wrong day, wrong number — each one is a missed appointment.
CRITICAL: list[str] = [
    "Booked. Dr Sarah Patel, Friday 14 March at 11:45 am.",
    "I have Thursday at 8 am, 9:15 am or 2:30 pm. Which suits?",
    "If this is an emergency, please hang up and call 000 now.",
    "The clinic is on 02 9456 1234 if you need us before then.",
    "Your appointment is at quarter past nine on Tuesday the 3rd.",
    "That's a standard consultation, about 15 minutes.",
    "We're at 42 Riverside Drive, Parramatta.",
    "Dr Patel is away until Monday the 21st of April.",
]

#: Short conversational turns. These dominate a real call, so their
#: time-to-first-audio is what the caller feels most often.
SHORT: list[str] = [
    "One moment while I check.",
    "Are you a new or existing patient?",
    "Sorry, I didn't catch that.",
    "What's your date of birth?",
    "Anything else?",
]


async def measure_latency(tts: PiperTTS, phrases: list[str]) -> tuple[list[float], list[float]]:
    first_audio: list[float] = []
    totals: list[float] = []
    for phrase in phrases:
        started = time.perf_counter()
        first: float | None = None
        async for _ in tts.synthesize_stream(for_speech(phrase)):
            if first is None:
                first = (time.perf_counter() - started) * 1000
        first_audio.append(first or 0.0)
        totals.append((time.perf_counter() - started) * 1000)
    return first_audio, totals


async def measure_intelligibility(
    tts: PiperTTS, stt, phrases: list[str], *, show: bool = False
) -> tuple[float, int]:  # noqa: ANN001
    """Speak each phrase, transcribe it back, and score the round trip."""
    rates: list[float] = []
    perfect = 0
    for phrase in phrases:
        spoken = for_speech(phrase)
        audio = await tts.synthesize(spoken)
        heard = (await stt.transcribe(audio)).text
        rate = word_error_rate(spoken, heard)
        rates.append(rate)
        if normalise(spoken) == normalise(heard):
            perfect += 1
        if show:
            colour = GREEN if rate == 0 else (YELLOW if rate < 0.25 else RED)
            print(f"    {colour}{rate * 100:5.1f}%{RESET} {spoken!r}")
            print(f"           {DIM}back  {heard.strip()!r}{RESET}")
    return (statistics.mean(rates) if rates else 0.0), perfect


def summarise(label: str, values: list[float], *, target: str = "") -> None:
    if not values:
        print(f"  {label:<24} —")
        return
    ordered = sorted(values)
    p95 = ordered[min(int(round(0.95 * (len(ordered) - 1))), len(ordered) - 1)]
    print(
        f"  {label:<24} median {statistics.median(values):6.0f} ms | "
        f"p95 {p95:6.0f} ms | max {max(values):6.0f} ms"
        + (f"   {DIM}target {target}{RESET}" if target else "")
    )


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--voices", default="", help="comma-separated voice names or paths")
    parser.add_argument("--show", action="store_true", help="print each round trip")
    parser.add_argument(
        "--no-intelligibility",
        action="store_true",
        help="latency only (skips loading the STT model)",
    )
    args = parser.parse_args()

    settings = get_settings()
    configure_logging(settings)

    voices = [v.strip() for v in args.voices.split(",") if v.strip()] or [settings.tts_voice]
    stt = None
    if not args.no_intelligibility:
        stt = build_stt(settings)
        if stt is None:
            print("STT must be configured for the intelligibility check.")
            return 2
        await stt.transcribe(audio_utils.silence(500))  # warm the weights

    print(f"{BOLD}TTS evaluation{RESET}")
    print(f"{DIM}phrases from the real response catalogue; synthetic values only{RESET}")
    if stt is not None:
        print(
            f"{YELLOW}Intelligibility is a Whisper round trip, not a listening test — "
            f"a proxy for whether details survive.{RESET}"
        )
    print()

    for voice in voices:
        path = Path(voice)
        if not path.is_absolute():
            path = REPO_ROOT / voice
        if not path.exists():
            print(f"{RED}{voice}: not found — download it into models/piper/{RESET}\n")
            continue

        tts = PiperTTS(path, length_scale=settings.tts_length_scale)
        await tts.warm_cache(SHORT[:1])

        print(f"{BOLD}{path.stem}{RESET}")
        short_first, short_total = await measure_latency(tts, SHORT)
        long_first, long_total = await measure_latency(tts, CRITICAL)
        summarise("first audio (short)", short_first, target="< 300 ms")
        summarise("first audio (detail)", long_first, target="< 300 ms")
        summarise("full synthesis (short)", short_total)
        summarise("full synthesis (detail)", long_total)

        audio_ms = 0.0
        for phrase in CRITICAL:
            audio_ms += audio_utils.duration_ms(await tts.synthesize(for_speech(phrase)))
        print(
            f"  {'real-time factor':<24} {sum(long_total) / max(audio_ms, 1):.3f}  "
            f"{DIM}(synthesis time per second of speech){RESET}"
        )

        if stt is not None:
            if args.show:
                print(f"  {DIM}detail phrases{RESET}")
            wer, perfect = await measure_intelligibility(tts, stt, CRITICAL, show=args.show)
            colour = GREEN if wer < 0.05 else (YELLOW if wer < 0.15 else RED)
            print(
                f"  {'intelligibility':<24} {colour}wer {wer * 100:.1f}%{RESET}  "
                f"exact {perfect}/{len(CRITICAL)}  {DIM}(round trip){RESET}"
            )
        print()

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
