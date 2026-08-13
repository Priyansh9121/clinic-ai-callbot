#!/usr/bin/env python
"""Does speech recognition still work once the audio has been through a phone?

    uv run python scripts/benchmark_telephony_stt.py
    uv run python scripts/benchmark_telephony_stt.py --show
    uv run python scripts/benchmark_telephony_stt.py --codec pcma

Milestone 3's accuracy numbers were measured on clean 16 kHz audio. A telephone
call is not that. This script puts the *same* phrases through the degradation a
real call applies and reports the difference:

    16 kHz clean speech          (what Milestone 3 measured)
        ↓  downsample
    8 kHz telephone band          (everything above ~3.4 kHz is gone)
        ↓  G.711 encode/decode
    8-bit companded              (~38 dB SNR, quantisation noise)
        ↓  upsample
    16 kHz canonical input        (what the pipeline actually feeds Whisper)
        ↓
    faster-whisper

Band-limiting is the part that matters. Consonants that distinguish names and
numbers — /s/, /f/, /θ/ — live largely above 3.4 kHz, which is exactly the
information a phone line discards.

The phrase sets are shared with scripts/benchmark_stt.py so clean and
telephone-band results are directly comparable. The safety set is the one that
decides whether this is releasable.

HONEST LIMITATION: the source is still synthetic Piper speech, and the
degradation is simulated rather than measured through Asterisk and a real SIP
leg. Packet loss, jitter, acoustic echo, handset microphones and background
noise are all absent, and every one of them makes real calls worse. These
numbers are an upper bound on telephone accuracy, not a field measurement.
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
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from app.core.config import get_settings  # noqa: E402
from app.core.logging import configure_logging  # noqa: E402
from app.speech import audio as audio_utils  # noqa: E402
from app.speech.factory import build_stt, build_tts  # noqa: E402
from app.telephony.transport import TelephonyFormat  # noqa: E402
from benchmark_stt import (  # noqa: E402
    BOOKING,
    EXTRACTORS,
    IDENTITY,
    SAFETY,
    Phrase,
    normalise,
    word_error_rate,
)

BOLD, DIM, GREEN, YELLOW, RED, RESET = (
    "\033[1m", "\033[2m", "\033[32m", "\033[33m", "\033[31m", "\033[0m"
)

#: Australian conversational speech, which is what a clinic line actually
#: receives. Deliberately includes elisions ("yeah nah") that a recogniser
#: trained on formal speech handles badly.
AUSTRALIAN: list[Phrase] = [
    Phrase("Friday arvo would be good", must_keep=("friday",)),
    Phrase("yeah nah, Thursday's better", must_keep=("thursday",)),
    Phrase("no worries, that suits me"),
    Phrase("half past two", must_keep=("half past",)),
    Phrase("quarter to four", must_keep=("quarter",)),
    # The practitioner's name is the clinically important token, not the
    # honorific — "Dr." and "Doctor" are the same thing to a booking flow.
    Phrase("can I see Doctor Nguyen", must_keep=("nguyen",)),
    Phrase("is Doctor Patel in this arvo", must_keep=("patel",)),
    Phrase("I'll come in on the weekend", must_keep=("weekend",)),
]

SETS: dict[str, list[Phrase]] = {
    "safety": SAFETY,
    "identity": IDENTITY,
    "booking": BOOKING,
    "australian": AUSTRALIAN,
}


def to_telephone_band(
    pcm16k: bytes, codec: TelephonyFormat, *, noise_dbfs: float | None = None
) -> bytes:
    """Apply the full telephone degradation and return canonical 16 kHz PCM.

    Uses the production conversion helpers, so this measures the codec path the
    call actually takes rather than an approximation of it.

    ``noise_dbfs`` adds broadband noise at the 8 kHz stage, standing in for line
    and handset noise. The codec path alone turns out to cost almost nothing on
    synthetic speech, which flatters it: a real call is never this quiet.
    """
    import numpy as np

    narrow = audio_utils.resample_pcm16(pcm16k, 16_000, 8_000)
    if noise_dbfs is not None:
        samples = np.frombuffer(narrow, dtype="<i2").astype(np.float32)
        amplitude = (10 ** (noise_dbfs / 20)) * 32768.0
        rng = np.random.default_rng(20260813)  # fixed: reruns are comparable
        noisy = samples + rng.normal(0.0, amplitude, samples.shape).astype(np.float32)
        narrow = np.clip(noisy, -32768, 32767).astype("<i2").tobytes()
    companded = codec.to_pcm16(codec.from_pcm16(narrow))
    return audio_utils.resample_pcm16(companded, 8_000, 16_000)


async def evaluate(
    stt, phrases: list[Phrase], rendered: list[bytes], *, show: bool = False
) -> dict:  # noqa: ANN001
    rates: list[float] = []
    latencies: list[float] = []
    exact = kept = checked = parsed_ok = parsed_checked = 0
    losses: list[tuple[str, str, tuple[str, ...]]] = []

    for phrase, clip in zip(phrases, rendered, strict=True):
        started = time.perf_counter()
        transcript = await stt.transcribe(clip)
        latencies.append((time.perf_counter() - started) * 1000)

        heard = normalise(transcript.text)
        rate = word_error_rate(phrase.text, transcript.text)
        rates.append(rate)
        if heard == normalise(phrase.text):
            exact += 1
        if show:
            colour = GREEN if rate == 0 else (YELLOW if rate < 0.3 else RED)
            print(f"    {colour}{rate * 100:5.1f}%{RESET} {phrase.text!r}")
            print(f"           {DIM}heard {transcript.text.strip()!r}{RESET}")
        if phrase.must_keep:
            checked += 1
            lost = tuple(k for k in phrase.must_keep if normalise(k) not in heard)
            if lost:
                losses.append((phrase.text, transcript.text, lost))
            else:
                kept += 1
        if phrase.expect is not None:
            kind, expected = phrase.expect
            parsed_checked += 1
            if EXTRACTORS[kind](heard) == expected:
                parsed_ok += 1

    return {
        "n": len(phrases),
        "wer": statistics.mean(rates) if rates else 0.0,
        "exact": exact,
        "median_ms": statistics.median(latencies) if latencies else 0.0,
        "keyword_checked": checked,
        "keyword_kept": kept,
        "parsed_checked": parsed_checked,
        "parsed_ok": parsed_ok,
        "losses": losses,
    }


def delta(clean: float, phone: float) -> str:
    change = phone - clean
    colour = GREEN if change <= 0.02 else (YELLOW if change < 0.15 else RED)
    return f"{colour}{change * 100:+5.1f}{RESET}"


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codec", default="pcmu", choices=["pcmu", "pcma"])
    parser.add_argument("--set", dest="which", default="all", choices=[*SETS, "all"])
    parser.add_argument("--show", action="store_true", help="print every transcript")
    parser.add_argument(
        "--noise",
        type=float,
        default=None,
        metavar="DBFS",
        help="add line noise at this level, e.g. -40 (quiet line) or -30 (poor line)",
    )
    args = parser.parse_args()

    settings = get_settings()
    configure_logging(settings)

    stt, tts = build_stt(settings), build_tts(settings)
    if stt is None or tts is None:
        print("STT and TTS must be configured; set them in .env")
        return 2

    codec = TelephonyFormat(args.codec)
    chosen = SETS if args.which == "all" else {args.which: SETS[args.which]}

    print(f"{BOLD}Telephone-band STT evaluation{RESET}")
    noise_note = f"  noise={args.noise:.0f} dBFS" if args.noise is not None else ""
    print(f"{DIM}model={settings.stt_model}  codec={codec.value}{noise_note}  "
          f"path: 16k -> 8k -> G.711 -> 8k -> 16k{RESET}")
    print(f"{YELLOW}Simulated degradation on synthetic speech. No packet loss, jitter,")
    print(f"echo or room noise — real calls will be worse than this.{RESET}\n")

    await stt.transcribe(audio_utils.silence(500))  # warm the weights

    totals: dict[str, tuple[dict, dict]] = {}
    for name, phrases in chosen.items():
        clean_audio = [await tts.synthesize(p.text) for p in phrases]
        phone_audio = [
            to_telephone_band(clip, codec, noise_dbfs=args.noise) for clip in clean_audio
        ]

        if args.show:
            print(f"  {DIM}{name} — clean{RESET}")
        clean = await evaluate(stt, phrases, clean_audio, show=args.show)
        if args.show:
            print(f"  {DIM}{name} — telephone band{RESET}")
        phone = await evaluate(stt, phrases, phone_audio, show=args.show)
        totals[name] = (clean, phone)

    print(f"{BOLD}{'set':<12}{'clean WER':>11}{'phone WER':>11}{'delta':>9}"
          f"{'clean kw':>10}{'phone kw':>10}{'phone ms':>10}{RESET}")
    for name, (clean, phone) in totals.items():
        clean_kw = (
            f"{clean['keyword_kept']}/{clean['keyword_checked']}"
            if clean["keyword_checked"]
            else "-"
        )
        phone_kw = (
            f"{phone['keyword_kept']}/{phone['keyword_checked']}"
            if phone["keyword_checked"]
            else "-"
        )
        degraded = (
            phone["keyword_checked"]
            and phone["keyword_kept"] < clean["keyword_kept"]
        )
        colour = RED if (degraded and name == "safety") else (YELLOW if degraded else GREEN)
        print(
            f"{name:<12}{clean['wer'] * 100:10.1f}%{phone['wer'] * 100:10.1f}%"
            f"{delta(clean['wer'], phone['wer']):>18}"
            f"{clean_kw:>10}{colour}{phone_kw:>10}{RESET}{phone['median_ms']:>9.0f}"
        )

    print()
    for name, (_, phone) in totals.items():
        for said, heard, lost in phone["losses"]:
            marker = RED if name == "safety" else YELLOW
            print(f"  {marker}{name}: lost {list(lost)}{RESET}  {DIM}{said!r} -> {heard!r}{RESET}")

    safety_clean, safety_phone = totals.get("safety", (None, None))
    if safety_phone is not None:
        kept, checked = safety_phone["keyword_kept"], safety_phone["keyword_checked"]
        print()
        if kept == checked:
            print(f"{GREEN}Safety keywords survive the telephone band: {kept}/{checked}.{RESET}")
        else:
            print(f"{RED}RELEASE BLOCKER: {checked - kept} safety phrase(s) lost over the")
            print(f"telephone band. See docs/security/threat-model.md T11.{RESET}")

    parsed = [(n, p) for n, (_, p) in totals.items() if p["parsed_checked"]]
    for name, phone in parsed:
        print(f"{DIM}{name}: production parsers recovered "
              f"{phone['parsed_ok']}/{phone['parsed_checked']} over the phone{RESET}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
