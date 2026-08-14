#!/usr/bin/env python
"""Show what the telephony stack is doing, without printing any secrets.

    uv run python scripts/telephony_status.py
    uv run python scripts/telephony_status.py --probe-media

Prints configuration, PBX reachability, media transport settings and active
call counts. Deliberately prints no ARI password, no SIP credential and no
caller number: this is the command you run while someone is watching your
screen, or paste into a bug report.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend"))

from app.core.config import get_settings  # noqa: E402
from app.core.logging import configure_logging  # noqa: E402
from app.telephony.destinations import UnsafeExtension  # noqa: E402
from app.telephony.factory import ari_client, destination_registry, wire_format  # noqa: E402
from app.telephony.transport import TelephonyFormat  # noqa: E402

BOLD, DIM, GREEN, YELLOW, RED, RESET = (
    "\033[1m",
    "\033[2m",
    "\033[32m",
    "\033[33m",
    "\033[31m",
    "\033[0m",
)


def mark(ok: bool, *, warn: bool = False) -> str:
    if warn:
        return f"{YELLOW}warn{RESET}"
    return f"{GREEN}ok{RESET}" if ok else f"{RED}down{RESET}"


async def probe_media(host: str, port: int) -> tuple[bool, str]:
    """Check whether something is already listening on the media port."""
    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=1.0)
        writer.close()
        await writer.wait_closed()
    except (TimeoutError, OSError) as exc:
        return False, type(exc).__name__
    return True, "accepting connections"


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--probe-media",
        action="store_true",
        help="try to connect to the media port (only meaningful with the app running)",
    )
    args = parser.parse_args()

    settings = get_settings()
    configure_logging(settings)

    print(f"{BOLD}Clinic AI — telephony status{RESET}")
    print(f"{DIM}no credentials or caller numbers are printed{RESET}\n")

    print(f"{BOLD}Configuration{RESET}")
    print(f"  {'enabled':<22} {settings.telephony_enabled}")
    print(f"  {'voice pipeline':<22} {mark(settings.voice_configured)}")
    print(f"  {'fully configured':<22} {mark(settings.telephony_configured)}")
    if not settings.telephony_configured:
        missing = []
        if not settings.telephony_enabled:
            missing.append("TELEPHONY_ENABLED")
        if not settings.voice_configured:
            missing.append("STT/TTS/VAD")
        if not settings.asterisk_ari_user:
            missing.append("ASTERISK_ARI_USER")
        if not settings.asterisk_ari_password.get_secret_value():
            missing.append("ASTERISK_ARI_PASSWORD")
        print(f"  {YELLOW}missing: {', '.join(missing)}{RESET}")

    print(f"\n{BOLD}PBX (Asterisk ARI){RESET}")
    print(f"  {'url':<22} {settings.asterisk_ari_url}")
    print(f"  {'user':<22} {settings.asterisk_ari_user or '(unset)'}")
    print(f"  {'password':<22} {'set' if settings.asterisk_ari_password.get_secret_value() else 'unset'}")
    print(f"  {'stasis app':<22} {settings.asterisk_app_name}")

    if settings.asterisk_ari_user and settings.asterisk_ari_password.get_secret_value():
        client = ari_client(settings)
        try:
            info = await asyncio.wait_for(client.asterisk_info(), timeout=3.0)
            build = (info.get("system") or {}).get("version", "unknown")
            print(f"  {'reachable':<22} {mark(True)}  {DIM}Asterisk {build}{RESET}")
        except Exception as exc:
            print(f"  {'reachable':<22} {mark(False)}  {DIM}{type(exc).__name__}{RESET}")
            print(f"  {DIM}is Asterisk running, and is ari.conf enabled?{RESET}")
        finally:
            await client.close()
    else:
        print(f"  {'reachable':<22} {DIM}not probed (no credentials configured){RESET}")

    print(f"\n{BOLD}Media transport (AudioSocket){RESET}")
    fmt = wire_format(settings)
    print(f"  {'bind (listen)':<22} {settings.telephony_media_host}:{settings.telephony_media_port}")
    advertised = f"{settings.media_advertise_host}:{settings.telephony_media_port}"
    routable = settings.media_advertise_is_routable
    print(f"  {'advertised to PBX':<22} {advertised}  {mark(routable)}")
    if not routable:
        print(f"  {RED}Asterisk cannot dial that. Set TELEPHONY_MEDIA_ADVERTISE_HOST to this"
              f" host's address on the PBX network.{RESET}")
    elif settings.media_advertise_host in ("127.0.0.1", "localhost", "::1"):
        print(f"  {DIM}loopback: correct only when Asterisk runs on this machine{RESET}")
    print(f"  {'wire format':<22} {fmt.value}  {DIM}{fmt.sample_rate} Hz, "
          f"{fmt.frame_bytes} bytes / 20 ms frame{RESET}")
    resampling = "none (matches canonical)" if fmt.sample_rate == 16_000 else (
        f"{fmt.sample_rate} <-> 16000 Hz, quality {settings.telephony_resample_quality}"
    )
    print(f"  {'resampling':<22} {resampling}")
    if settings.telephony_media_host not in ("127.0.0.1", "localhost", "::1"):
        print(f"  {YELLOW}not bound to loopback — AudioSocket has no authentication, "
              f"so this port must be on a private network{RESET}")
    if args.probe_media:
        listening, detail = await probe_media(
            settings.telephony_media_host, settings.telephony_media_port
        )
        print(f"  {'listening':<22} {mark(listening)}  {DIM}{detail}{RESET}")

    print(f"\n{BOLD}Transfer destinations{RESET}")
    try:
        registry = destination_registry(settings)
        print(f"  {'context':<22} {registry.context}")
        for destination in sorted(registry.configured(), key=str):
            target = registry.resolve(destination)
            print(f"  {destination.value.lower():<22} extension {target.extension}")
        print(f"  {DIM}only these can be dialled; caller speech cannot reach a dial string{RESET}")
    except UnsafeExtension as exc:
        print(f"  {RED}refused: {exc}{RESET}")

    print(f"\n{BOLD}Capacity{RESET}")
    print(f"  {'max active calls':<22} {settings.telephony_max_active_calls}")
    print(f"  {'at capacity':<22} {settings.telephony_capacity_policy}")
    print(f"  {'hangup drain':<22} {settings.telephony_hangup_drain_ms} ms")
    print(f"\n{DIM}Live call counts appear in GET /health while the app is running.{RESET}")
    print(f"{DIM}Recording is disabled; no audio or transcripts are retained.{RESET}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
