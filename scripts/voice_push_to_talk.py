"""Push-to-talk debug mode: record on Enter, transcribe, respond.

    uv run python scripts/voice_chat.py --push-to-talk

A diagnostic tool, not the product. It bypasses VAD and endpointing entirely so
that when something feels wrong you can tell *which* layer is wrong:

* Push-to-talk transcribes correctly, but normal mode does not
    → VAD or endpointing (thresholds, echo, clipped starts).
* Push-to-talk transcribes badly too
    → STT (model size, microphone level, accent, background noise).

There is no barge-in here, because there is no continuous listening — that is
the point of the isolation.

Privacy is unchanged: audio stays in memory and is discarded after each turn.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend"))

from app.clinic.knowledge_data import build_dev_knowledge  # noqa: E402
from app.conversation.engine import ConversationEngine  # noqa: E402
from app.conversation.store import build_session_store  # noqa: E402
from app.core.config import get_settings  # noqa: E402
from app.core.logging import configure_logging  # noqa: E402
from app.database.session import dispose_engine, session_scope  # noqa: E402
from app.llm.factory import build_llm_provider, build_message_analyser  # noqa: E402
from app.safety.engine import SafetyEngine  # noqa: E402
from app.speech.audio import duration_ms, rms  # noqa: E402
from app.speech.factory import build_stt, build_tts  # noqa: E402
from app.voice.devices import MicrophoneAudioInput, SpeakerAudioOutput  # noqa: E402
from app.voice.speech_text import for_speech  # noqa: E402

BOLD, DIM, CYAN, RED, RESET = "\033[1m", "\033[2m", "\033[36m", "\033[31m", "\033[0m"


async def _record_until_enter(microphone: MicrophoneAudioInput) -> bytes:
    """Capture frames until the user presses Enter again."""
    captured = bytearray()
    done = asyncio.Event()

    async def wait_for_enter() -> None:
        await asyncio.to_thread(sys.stdin.readline)
        done.set()

    waiter = asyncio.create_task(wait_for_enter())
    try:
        async for frame in microphone.frames():
            captured.extend(frame)
            if done.is_set():
                break
    finally:
        waiter.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await waiter
    return bytes(captured)


async def run_push_to_talk(args: argparse.Namespace) -> int:
    settings = get_settings()
    configure_logging(settings)

    if settings.stt_provider == "none" or settings.tts_provider == "none":
        print(f"{RED}STT and TTS must be configured for push-to-talk.{RESET}")
        return 2

    stt = build_stt(settings)
    tts = build_tts(settings)
    store = build_session_store(settings.redis_url, settings.session_ttl_seconds)
    llm_provider = build_llm_provider(settings)
    analyser = build_message_analyser(settings, provider=llm_provider)

    microphone = MicrophoneAudioInput(
        device=settings.device_or_default(settings.voice_input_device)
    )
    speaker = SpeakerAudioOutput(device=settings.device_or_default(settings.voice_output_device))

    print(f"{BOLD}Push-to-talk (VAD bypassed){RESET}")
    print(f"{DIM}Enter to start recording, Enter again to stop. Ctrl+C to quit.{RESET}\n")

    try:
        async with session_scope() as db:
            engine = ConversationEngine(
                db=db,
                settings=settings,
                store=store,
                safety=SafetyEngine(),
                analyser=analyser,
                knowledge=build_dev_knowledge(),
            )
            greeting = await engine.start(channel="voice")
            session_id = greeting.session_id
            await speaker.start()
            print(f"{CYAN}{BOLD}Clinic:{RESET} {greeting.reply}")
            await speaker.play(await tts.synthesize(for_speech(greeting.reply)))
            await speaker.wait_until_drained()

            while True:
                input(f"\n{DIM}[Enter to speak]{RESET}")
                await microphone.start()
                print(f"{DIM}recording… [Enter to stop]{RESET}")
                audio = await _record_until_enter(microphone)
                await microphone.stop()

                print(f"{DIM}captured {duration_ms(audio):.0f} ms, level {rms(audio):.4f}{RESET}")
                transcript = await stt.transcribe(audio)
                if not transcript.text.strip():
                    print(f"{RED}(nothing recognised){RESET}")
                    continue
                print(f"{BOLD}You:{RESET} {transcript.text}  {DIM}({transcript.latency_ms:.0f}ms)"
                      f"{RESET}")

                result = await engine.handle_message(session_id, transcript.text)
                print(f"{CYAN}{BOLD}Clinic:{RESET} {result.reply}")
                await speaker.play(await tts.synthesize(for_speech(result.reply)))
                await speaker.wait_until_drained()
                if result.ended:
                    break
    except (KeyboardInterrupt, EOFError):  # pragma: no cover - interactive only
        print(f"\n{DIM}hanging up…{RESET}")
    finally:
        await microphone.stop()
        await speaker.stop()
        await store.close()
        if llm_provider is not None and hasattr(llm_provider, "aclose"):
            await llm_provider.aclose()
        await dispose_engine()
    return 0
