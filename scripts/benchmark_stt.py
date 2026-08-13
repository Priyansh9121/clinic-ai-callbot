#!/usr/bin/env python
"""Measure local STT accuracy and latency on clinic speech.

    uv run python scripts/benchmark_stt.py
    uv run python scripts/benchmark_stt.py --models tiny.en,base.en,small.en
    uv run python scripts/benchmark_stt.py --set safety

Three phrase sets, because the failure modes are not equally serious:

    safety    misrecognition here can hurt someone
    identity  names, dates of birth, phone numbers — wrong data, wrong patient
    booking   ordinary scheduling speech

Word error rate is reported, but the number that decides the model is
**safety-keyword recall**: whether "chest pain" survived transcription at all.
A model with a better WER that drops "can't breathe" is the worse model.

HONEST LIMITATION, stated up front and repeated in the output: the caller audio
here is synthesised with Piper, because Milestone 3 records no real speech
(docs/privacy/). Synthetic speech is cleaner, better-enunciated and noise-free,
so **every accuracy number below is optimistic**. Real callers on a real phone
line — accents, mobiles, background noise, 8 kHz codecs — will do worse. Treat
these as an upper bound and a regression detector, not as a field measurement.
"""

from __future__ import annotations

import argparse
import asyncio
import re
import statistics
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend"))

from app.conversation.nlu.baseline import _extract_dob, _extract_mobile  # noqa: E402
from app.core.config import get_settings  # noqa: E402
from app.core.logging import configure_logging  # noqa: E402
from app.speech import audio as audio_utils  # noqa: E402
from app.speech.factory import build_tts  # noqa: E402
from app.speech.stt.whisper import FasterWhisperSTT  # noqa: E402

BOLD, DIM, GREEN, YELLOW, RED, RESET = (
    "\033[1m", "\033[2m", "\033[32m", "\033[33m", "\033[31m", "\033[0m"
)


class Phrase:
    """One evaluation item: what is said, and what must survive."""

    def __init__(
        self,
        text: str,
        *,
        must_keep: tuple[str, ...] = (),
        expect: tuple[str, str] | None = None,
    ) -> None:
        self.text = text
        #: Substrings whose loss changes the meaning. Checked after
        #: normalisation, so "Chest pain!" still matches "chest pain".
        self.must_keep = must_keep
        #: ``(extractor, expected)`` — run the *production* parser over the
        #: transcript and check it recovers this value. Whisper writes "14 March
        #: 1958" where the caller said "fourteen March nineteen fifty eight";
        #: WER calls that 80% wrong, but the booking flow does not care how the
        #: words were spelled, only whether the date came out right.
        self.expect = expect


#: The real parsers from the text pipeline, so this measures the system's
#: tolerance of STT output rather than a benchmark's private idea of correct.
EXTRACTORS = {
    "dob": lambda text: (lambda d: d.isoformat() if d else None)(_extract_dob(text)),
    "mobile": _extract_mobile,
}


#: Phrases where a transcription error has clinical consequences. These are the
#: inputs the deterministic SafetyEngine keys on, so if STT drops them the
#: safety net never sees them — the residual risk documented in
#: docs/security/threat-model.md.
SAFETY: list[Phrase] = [
    Phrase("I'm having chest pain", must_keep=("chest pain",)),
    Phrase("I can't breathe properly", must_keep=("breathe",)),
    Phrase("my chest feels really tight and my arm hurts", must_keep=("chest",)),
    Phrase("I think I'm having a heart attack", must_keep=("heart attack",)),
    Phrase("he's unconscious and not responding", must_keep=("unconscious",)),
    Phrase("I'm bleeding heavily and it won't stop", must_keep=("bleeding",)),
    Phrase("I'm allergic to penicillin", must_keep=("allergic", "penicillin")),
    Phrase("she's having trouble breathing", must_keep=("breathing",)),
    Phrase("I took too many tablets", must_keep=("too many",)),
    Phrase("my face has gone numb on one side", must_keep=("numb",)),
]

#: Getting these wrong books the wrong patient, or makes the patient
#: unreachable. Australian formats throughout — see §38/§39.
IDENTITY: list[Phrase] = [
    Phrase("my name is Margaret Hughes", must_keep=("hughes",)),
    Phrase("Ng, that's N for November, G for golf", must_keep=("n", "g")),
    Phrase("Siobhan O'Brien", must_keep=("brien",)),
    Phrase("fourteen March nineteen fifty eight", expect=("dob", "1958-03-14")),
    Phrase("the third of March nineteen eighty two", expect=("dob", "1982-03-03")),
    Phrase(
        "my mobile is oh four one two, three four five, six seven eight",
        expect=("mobile", "0412345678"),
    ),
    Phrase("my Medicare number is two three four five, six seven eight nine"),
    Phrase("I live in Woolloomooloo"),
    Phrase("it's Nguyen, N G U Y E N", must_keep=("n", "g")),
    Phrase("Dr Patel's rooms in Parramatta", must_keep=("patel",)),
]

#: Ordinary scheduling speech. Errors here cost a repeated question, not harm.
BOOKING: list[Phrase] = [
    Phrase("I'd like to book an appointment please"),
    Phrase("do you have anything on Thursday afternoon", must_keep=("thursday",)),
    Phrase("can I see Doctor Patel next week", must_keep=("patel",)),
    Phrase("I need to cancel my appointment", must_keep=("cancel",)),
    Phrase("can we move it to a later time"),
    Phrase("quarter past nine would suit me"),
    Phrase("I've been to the clinic before"),
    Phrase("it's a standard consultation"),
    Phrase("what time do you close on Saturday", must_keep=("saturday",)),
    Phrase("no, the morning is better"),
]

SETS: dict[str, list[Phrase]] = {"safety": SAFETY, "identity": IDENTITY, "booking": BOOKING}


def normalise(text: str) -> str:
    """Compare meaning, not punctuation or casing."""
    text = text.lower().replace("'", "'")
    text = re.sub(r"[^a-z0-9' ]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def word_error_rate(reference: str, hypothesis: str) -> float:
    """Standard Levenshtein WER over words."""
    ref, hyp = normalise(reference).split(), normalise(hypothesis).split()
    if not ref:
        return 0.0 if not hyp else 1.0
    previous = list(range(len(hyp) + 1))
    for i, ref_word in enumerate(ref, start=1):
        current = [i]
        for j, hyp_word in enumerate(hyp, start=1):
            current.append(
                previous[j - 1]
                if ref_word == hyp_word
                else 1 + min(previous[j - 1], previous[j], current[j - 1])
            )
        previous = current
    return previous[-1] / len(ref)


async def evaluate(  # noqa: ANN001
    stt, phrases: list[Phrase], rendered: list[bytes], *, show: bool = False
) -> dict:
    exact = 0
    kept = 0
    checked = 0
    parsed_ok = 0
    parsed_checked = 0
    parse_failures: list[tuple[str, str, str, object]] = []
    rates: list[float] = []
    latencies: list[float] = []
    misses: list[tuple[str, str, tuple[str, ...]]] = []

    for phrase, audio in zip(phrases, rendered, strict=True):
        started = time.perf_counter()
        transcript = await stt.transcribe(audio)
        latencies.append((time.perf_counter() - started) * 1000)

        heard = normalise(transcript.text)
        rate = word_error_rate(phrase.text, transcript.text)
        rates.append(rate)
        if show:
            marker = GREEN if rate == 0 else YELLOW
            print(f"    {marker}{rate * 100:5.1f}%{RESET} {phrase.text!r}")
            print(f"           {DIM}heard {transcript.text.strip()!r}{RESET}")
        if heard == normalise(phrase.text):
            exact += 1
        if phrase.expect is not None:
            kind, expected = phrase.expect
            parsed_checked += 1
            got = EXTRACTORS[kind](normalise(transcript.text))
            if got == expected:
                parsed_ok += 1
            else:
                parse_failures.append((phrase.text, kind, expected, got))

        if phrase.must_keep:
            checked += 1
            lost = tuple(k for k in phrase.must_keep if normalise(k) not in heard)
            if lost:
                misses.append((phrase.text, transcript.text, lost))
            else:
                kept += 1

    return {
        "n": len(phrases),
        "exact": exact,
        "wer": statistics.mean(rates) if rates else 0.0,
        "median_ms": statistics.median(latencies) if latencies else 0.0,
        "keyword_checked": checked,
        "keyword_kept": kept,
        "misses": misses,
        "parsed_checked": parsed_checked,
        "parsed_ok": parsed_ok,
        "parse_failures": parse_failures,
    }


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--models", default="", help="comma-separated, e.g. tiny.en,base.en")
    parser.add_argument("--set", dest="which", default="all", choices=[*SETS, "all"])
    parser.add_argument(
        "--show",
        action="store_true",
        help="print every reference and hypothesis (synthetic phrases only)",
    )
    args = parser.parse_args()

    settings = get_settings()
    configure_logging(settings)

    tts = build_tts(settings)
    if tts is None:
        print("TTS must be configured — it renders the evaluation audio.")
        return 2

    models = [m.strip() for m in args.models.split(",") if m.strip()] or [settings.stt_model]
    chosen = SETS if args.which == "all" else {args.which: SETS[args.which]}

    print(f"{BOLD}STT evaluation{RESET}")
    print(f"{YELLOW}Synthetic caller audio (Piper). Cleaner than a real phone line —")
    print(f"these accuracy numbers are an upper bound, not a field result.{RESET}\n")

    rendered: dict[str, list[bytes]] = {}
    for name, phrases in chosen.items():
        rendered[name] = [await tts.synthesize(p.text) for p in phrases]

    for model in models:
        stt = FasterWhisperSTT(
            model=model,
            compute_type=settings.stt_compute_type,
            language=settings.stt_language,
            beam_size=settings.stt_beam_size,
            download_root=str(REPO_ROOT / "models" / "whisper"),
        )
        # Warm the weights so the first phrase is not charged for the load.
        await stt.transcribe(audio_utils.silence(500))

        print(f"{BOLD}{model}{RESET}")
        for name, phrases in chosen.items():
            if args.show:
                print(f"  {DIM}{name}{RESET}")
            report = await evaluate(stt, phrases, rendered[name], show=args.show)
            recall = (
                f"{report['keyword_kept']}/{report['keyword_checked']}"
                if report["keyword_checked"]
                else "—"
            )
            colour = GREEN
            if report["keyword_checked"] and report["keyword_kept"] < report["keyword_checked"]:
                colour = RED if name == "safety" else YELLOW
            print(
                f"  {name:<9} wer {report['wer'] * 100:5.1f}%  "
                f"exact {report['exact']}/{report['n']}  "
                f"{colour}keywords {recall}{RESET}  "
                f"{DIM}median {report['median_ms']:.0f} ms{RESET}"
            )
            if report["parsed_checked"]:
                colour = (
                    GREEN if report["parsed_ok"] == report["parsed_checked"] else RED
                )
                print(
                    f"  {'':<9} {colour}parsers recovered "
                    f"{report['parsed_ok']}/{report['parsed_checked']}{RESET}  "
                    f"{DIM}(dob/mobile, via the real extractors){RESET}"
                )
            for said, heard, lost in report["misses"]:
                print(f"    {RED}lost {list(lost)}{RESET}  {DIM}{said!r} -> {heard!r}{RESET}")
            for said, kind, expected, got in report["parse_failures"]:
                print(f"    {RED}{kind} {got!r} != {expected!r}{RESET}  {DIM}{said!r}{RESET}")
        print()

    print(f"{DIM}Keyword recall is the deciding metric: a lower WER is not worth")
    print(f"dropping a safety phrase. Residual risk: docs/security/threat-model.md{RESET}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
