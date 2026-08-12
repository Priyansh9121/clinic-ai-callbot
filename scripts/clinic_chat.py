#!/usr/bin/env python
"""Terminal chat simulator.

    uv run python scripts/clinic_chat.py             # in-process engine
    uv run python scripts/clinic_chat.py --api       # via the running HTTP API

Drives the *real* ``ConversationEngine`` — there is no simulator-specific
conversation logic. What you see here is exactly what a caller will get once
speech is attached in Milestone 3.

Commands:  /state  show conversation memory   /new  restart   /quit  exit
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.clinic.knowledge_data import build_dev_knowledge  # noqa: E402
from app.conversation.engine import ConversationEngine, TurnResult  # noqa: E402
from app.conversation.nlu.baseline import BaselineAnalyser  # noqa: E402
from app.conversation.store import build_session_store  # noqa: E402
from app.core.config import get_settings  # noqa: E402
from app.core.logging import configure_logging  # noqa: E402
from app.database.session import dispose_engine, session_scope  # noqa: E402
from app.safety.engine import SafetyEngine  # noqa: E402

BOLD, DIM, CYAN, YELLOW, RED, RESET = (
    "\033[1m", "\033[2m", "\033[36m", "\033[33m", "\033[31m", "\033[0m"
)


def render(result: TurnResult, *, show_meta: bool) -> None:
    colour = RED if result.emergency else YELLOW if result.requires_human else CYAN
    print(f"\n{colour}{BOLD}Clinic:{RESET}\n{result.reply}")
    if show_meta:
        flags = []
        if result.requires_human:
            flags.append(f"transfer:{result.transfer_reason}")
        if result.emergency:
            flags.append("EMERGENCY")
        if result.ended:
            flags.append("ended")
        suffix = ("  " + " ".join(flags)) if flags else ""
        print(
            f"{DIM}[{result.state} | {result.intent} | "
            f"{result.latency_ms:.0f}ms]{suffix}{RESET}"
        )


def read_input() -> str | None:
    try:
        return input(f"\n{BOLD}You:{RESET}\n").strip()
    except (EOFError, KeyboardInterrupt):
        return None


# --- In-process mode --------------------------------------------------------
async def run_local(show_meta: bool) -> int:
    settings = get_settings()
    configure_logging(settings)

    store = build_session_store(settings.redis_url, settings.session_ttl_seconds)
    safety = SafetyEngine()
    analyser = BaselineAnalyser()
    knowledge = build_dev_knowledge()

    def engine_for(db) -> ConversationEngine:  # noqa: ANN001
        return ConversationEngine(
            db=db,
            settings=settings,
            store=store,
            safety=safety,
            analyser=analyser,
            knowledge=knowledge,
        )

    print(f"{DIM}in-process engine · session store: {settings.session_store_backend}{RESET}")
    try:
        # A fresh session per turn mirrors the request lifecycle the API uses,
        # so the simulator exercises the same transaction boundaries.
        async with session_scope() as db:
            start = await engine_for(db).start(channel="simulator")
        session_id = start.session_id
        render(start, show_meta=show_meta)

        while True:
            text = read_input()
            if text is None or text.lower() in ("/quit", "/exit"):
                break
            if not text:
                continue
            if text.lower() == "/state":
                async with session_scope() as db:
                    session = await engine_for(db).get_session(session_id)
                print(
                    f"{DIM}{json.dumps(session.memory_snapshot(), indent=2) if session else 'no session'}{RESET}"
                )
                continue
            if text.lower() == "/new":
                async with session_scope() as db:
                    start = await engine_for(db).start(channel="simulator")
                session_id = start.session_id
                render(start, show_meta=show_meta)
                continue

            async with session_scope() as db:
                result = await engine_for(db).handle_message(session_id, text)
            render(result, show_meta=show_meta)
            if result.ended:
                print(f"{DIM}— call ended —{RESET}")
                break

        async with session_scope() as db:
            await engine_for(db).end(session_id)
    finally:
        await store.close()
        await dispose_engine()
    return 0


# --- HTTP mode --------------------------------------------------------------
def _post(base: str, path: str, payload: dict) -> dict:
    request = urllib.request.Request(
        base + path,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return {"reply": f"[HTTP {exc.code}] {exc.read().decode()[:200]}",
                "state": "ERROR", "intent": "UNKNOWN"}
    except urllib.error.URLError as exc:
        raise SystemExit(f"cannot reach {base}: {exc.reason}") from exc


def run_api(base: str, show_meta: bool) -> int:
    print(f"{DIM}via API at {base}{RESET}")
    start = _post(base, "/api/conversation/start", {"channel": "simulator"})
    session_id = start["session_id"]
    print(f"\n{CYAN}{BOLD}Clinic:{RESET}\n{start['reply']}")

    while True:
        text = read_input()
        if text is None or text.lower() in ("/quit", "/exit"):
            break
        if not text:
            continue
        data = _post(base, "/api/conversation/message",
                     {"session_id": session_id, "message": text})
        colour = RED if data.get("emergency") else YELLOW if data.get("requires_human") else CYAN
        print(f"\n{colour}{BOLD}Clinic:{RESET}\n{data['reply']}")
        if show_meta:
            print(f"{DIM}[{data['state']} | {data['intent']} | "
                  f"{data.get('latency_ms', 0):.0f}ms]{RESET}")
        if data.get("ended"):
            print(f"{DIM}— call ended —{RESET}")
            break
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api", action="store_true", help="drive the HTTP API instead")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--quiet", action="store_true", help="hide state/latency annotations")
    args = parser.parse_args()

    print(f"{BOLD}Riverside Medical Clinic — conversation simulator{RESET}")
    print(f"{DIM}FICTIONAL development data. /state /new /quit{RESET}")

    if args.api:
        return run_api(args.base_url.rstrip("/"), show_meta=not args.quiet)
    return asyncio.run(run_local(show_meta=not args.quiet))


if __name__ == "__main__":
    raise SystemExit(main())
