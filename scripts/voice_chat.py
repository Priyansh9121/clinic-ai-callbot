#!/usr/bin/env python
"""Talk to the clinic receptionist through the local microphone and speakers.

    uv run python scripts/voice_chat.py
    uv run python scripts/voice_chat.py --devices          # list audio devices
    uv run python scripts/voice_chat.py --show-transcript  # developer mode
    uv run python scripts/voice_chat.py --push-to-talk     # bypass VAD

Speak naturally; interrupt the agent whenever you like. Ctrl+C exits cleanly.

**Use headphones.** The pipeline listens while it speaks — that is what makes
barge-in work — so on open speakers the agent can hear its own voice and
interrupt itself. See docs/architecture/voice-pipeline.md.

Privacy: nothing is recorded and no transcript is stored. ``--show-transcript``
prints what was heard to *your terminal only*, for development against the
synthetic clinic; it writes nothing to disk or to the logs.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import signal
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend"))

from app.clinic.knowledge_data import build_dev_knowledge  # noqa: E402
from app.conversation.engine import ConversationEngine  # noqa: E402
from app.conversation.nlu.baseline import BaselineAnalyser  # noqa: E402
from app.conversation.store import build_session_store  # noqa: E402
from app.core.config import get_settings  # noqa: E402
from app.core.logging import configure_logging  # noqa: E402
from app.database.session import dispose_engine, session_scope  # noqa: E402
from app.llm.factory import build_llm_provider, build_message_analyser  # noqa: E402
from app.safety.engine import SafetyEngine  # noqa: E402
from app.voice.devices import default_devices, list_devices  # noqa: E402
from app.voice.factory import build_voice_session  # noqa: E402
from app.voice.states import VoiceState  # noqa: E402

BOLD, DIM, CYAN, GREEN, YELLOW, RED, RESET = (
    "\033[1m", "\033[2m", "\033[36m", "\033[32m", "\033[33m", "\033[31m", "\033[0m"
)

STATE_COLOUR = {
    VoiceState.LISTENING.value: GREEN,
    VoiceState.SPEECH_DETECTED.value: CYAN,
    VoiceState.TRANSCRIBING.value: CYAN,
    VoiceState.THINKING.value: YELLOW,
    VoiceState.SPEAKING.value: BOLD,
    VoiceState.INTERRUPTED.value: RED,
}


def print_devices() -> int:
    devices = list_devices()
    if not devices:
        print("no audio devices found (is PortAudio installed?)")
        return 1
    default_in, default_out = default_devices()
    print(f"{BOLD}Audio devices{RESET}")
    for device in devices:
        marks = []
        if device["index"] == default_in:
            marks.append("default in")
        if device["index"] == default_out:
            marks.append("default out")
        kinds = []
        if device["inputs"]:
            kinds.append(f"in:{device['inputs']}")
        if device["outputs"]:
            kinds.append(f"out:{device['outputs']}")
        suffix = f"  {DIM}({', '.join(marks)}){RESET}" if marks else ""
        print(f"  [{device['index']}] {device['name'][:44]:44} {','.join(kinds):12}{suffix}")
    return 0


class Console:
    """Renders session events. Knows nothing about audio."""

    def __init__(self, *, show_transcript: bool, show_state: bool) -> None:
        self._show_transcript = show_transcript
        self._show_state = show_state

    def __call__(self, kind: str, payload: dict) -> None:
        if kind == "state" and self._show_state:
            state = str(payload.get("state"))
            colour = STATE_COLOUR.get(state, DIM)
            print(f"{colour}[{state}]{RESET}", flush=True)
        elif kind == "barge_in":
            print(f"{RED}— interrupted ({payload.get('stop_ms')} ms) —{RESET}", flush=True)
        elif kind == "transcript":
            if self._show_transcript:
                print(f"{BOLD}You:{RESET} {payload.get('text')}", flush=True)
            else:
                print(f"{DIM}(heard {payload.get('chars')} characters){RESET}", flush=True)
        elif kind == "turn":
            result = payload.get("result")
            stats = payload.get("metrics")
            if result is not None:
                print(f"{CYAN}{BOLD}Clinic:{RESET} {result.reply}", flush=True)
            if stats is not None:
                print(
                    f"{DIM}  stt {stats.stt_ms:.0f}ms · engine {stats.engine_ms:.0f}ms · "
                    f"tts {stats.tts_first_audio_ms:.0f}ms · "
                    f"first audio {stats.time_to_first_audio_ms:.0f}ms"
                    f"{' · acknowledged' if stats.acknowledged else ''}{RESET}",
                    flush=True,
                )


async def run(args: argparse.Namespace) -> int:
    settings = get_settings()
    configure_logging(settings)

    if not settings.voice_configured:
        print(f"{RED}Voice is not configured.{RESET}")
        print("Set these in .env, then re-run:")
        print("  STT_PROVIDER=faster_whisper")
        print("  TTS_PROVIDER=piper")
        print("  VAD_PROVIDER=silero")
        return 2

    store = build_session_store(settings.redis_url, settings.session_ttl_seconds)
    llm_provider = build_llm_provider(settings)
    analyser = build_message_analyser(settings, provider=llm_provider)
    knowledge = build_dev_knowledge()
    safety = SafetyEngine()

    default_in, default_out = default_devices()
    devices = {d["index"]: d["name"] for d in list_devices()}
    print(f"{BOLD}Riverside Medical Clinic — voice{RESET}")
    print(f"{DIM}microphone : {devices.get(default_in, 'default')}")
    print(f"speaker    : {devices.get(default_out, 'default')}")
    print(f"stt        : {settings.stt_provider} ({settings.stt_model})")
    print(f"tts        : {settings.tts_provider} ({Path(settings.tts_voice).stem})")
    print(f"vad        : {settings.vad_provider}")
    print(f"llm        : {settings.llm_provider} mode={settings.effective_llm_mode}")
    print(f"endpointing: {settings.voice_end_silence_ms} ms end-silence")
    print(f"use headphones — the agent listens while it speaks{RESET}")
    print(f"{DIM}Ctrl+C to hang up. FICTIONAL development data.{RESET}\n")

    session = None
    try:
        async with session_scope() as db:
            engine = ConversationEngine(
                db=db,
                settings=settings,
                store=store,
                safety=safety,
                analyser=analyser,
                knowledge=knowledge,
            )
            session = build_voice_session(
                settings=settings,
                engine=engine,
                greet=not args.no_greeting,
                on_event=Console(
                    show_transcript=args.show_transcript, show_state=args.show_state
                ),
            )

            loop = asyncio.get_running_loop()
            for sig in (signal.SIGINT, signal.SIGTERM):
                with contextlib.suppress(NotImplementedError):
                    loop.add_signal_handler(
                        sig, lambda: asyncio.create_task(session.stop())
                    )

            await session.run()
    except KeyboardInterrupt:  # pragma: no cover - interactive only
        print(f"\n{DIM}hanging up…{RESET}")
    finally:
        if session is not None and session.turn_metrics:
            _print_summary(session)
        await store.close()
        if llm_provider is not None and hasattr(llm_provider, "aclose"):
            await llm_provider.aclose()
        await dispose_engine()
    return 0


def _print_summary(session) -> None:  # noqa: ANN001
    stats = session.turn_metrics
    def median(values: list[float]) -> float:
        ordered = sorted(values)
        return ordered[len(ordered) // 2] if ordered else 0.0

    print(f"\n{BOLD}Call summary{RESET}  ({len(stats)} turns)")
    print(
        f"{DIM}  median  stt {median([s.stt_ms for s in stats]):.0f}ms · "
        f"engine {median([s.engine_ms for s in stats]):.0f}ms · "
        f"first audio {median([s.time_to_first_audio_ms for s in stats]):.0f}ms{RESET}"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--devices", action="store_true", help="list audio devices and exit")
    parser.add_argument(
        "--show-transcript",
        action="store_true",
        help="print what was heard (development only; nothing is stored)",
    )
    parser.add_argument("--show-state", action="store_true", help="print voice state changes")
    parser.add_argument("--no-greeting", action="store_true", help="do not speak first")
    parser.add_argument(
        "--push-to-talk",
        action="store_true",
        help="press Enter to start/stop an utterance instead of using VAD "
        "(debug aid for separating STT problems from endpointing problems)",
    )
    args = parser.parse_args()

    if args.devices:
        return print_devices()
    if args.push_to_talk:
        from scripts.voice_push_to_talk import run_push_to_talk

        return asyncio.run(run_push_to_talk(args))
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
