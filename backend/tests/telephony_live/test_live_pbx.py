"""Checks that need a running Asterisk.

    uv run pytest -m telephony_live

Skipped by default: ``uv run pytest`` must pass on a machine with no PBX, so
nothing in CI depends on one.

These verify the things a test double cannot — that this Asterisk build has the
modules we rely on, that our ARI credentials work, and that the AudioSocket
channel driver is actually present. They do **not** place a call: a call needs
a registered softphone and a human to speak, which is the manual acceptance
test in docs/development/local-sip.md.

Point them at a PBX with:
    ASTERISK_ARI_URL, ASTERISK_ARI_USER, ASTERISK_ARI_PASSWORD
"""

from __future__ import annotations

import pytest

from app.core.config import get_settings
from app.telephony.ari import AriError
from app.telephony.factory import ari_client

pytestmark = pytest.mark.telephony_live


#: Placeholder values shipped in .env.example. Treated as "not configured"
#: rather than "wrong password", so the suite skips instead of failing.
_PLACEHOLDERS = {"", "CHANGE_ME", "change-me", "changeme"}


@pytest.fixture(scope="module")
def settings():
    config = get_settings()
    if (
        config.asterisk_ari_user in _PLACEHOLDERS
        or config.asterisk_ari_password.get_secret_value() in _PLACEHOLDERS
    ):
        pytest.skip("ARI credentials are placeholders — set them to run live PBX tests")
    return config


@pytest.fixture
async def client(settings):
    ari = ari_client(settings)
    if not await ari.ping():
        await ari.close()
        pytest.skip(f"no Asterisk reachable at {ari.safe_url}")
    try:
        yield ari
    finally:
        await ari.close()


async def test_ari_credentials_are_accepted(client) -> None:
    """A 401 here means ari.conf and the .env disagree."""
    try:
        info = await client.asterisk_info()
    except AriError as exc:
        pytest.fail(f"ARI rejected the configured credentials: {exc}")

    assert "system" in info or "build" in info


async def test_the_asterisk_version_is_recorded(client) -> None:
    info = await client.asterisk_info()
    version = (info.get("system") or {}).get("version")

    assert version, "Asterisk did not report a version"
    print(f"\nAsterisk version: {version}")


async def test_audiosocket_is_available(client) -> None:
    """Both forms are needed: the app for dialplan use, the driver for ARI.

    Without ``chan_audiosocket`` the originate in TelephonyService fails and
    every call connects then hears silence — a failure that is invisible until
    someone actually rings the number.
    """
    names = {module["name"] for module in await client.get_json("/asterisk/modules")}

    assert "res_audiosocket.so" in names, "res_audiosocket is not loaded"
    assert "chan_audiosocket.so" in names, (
        "chan_audiosocket is not loaded — ARI cannot originate a media channel"
    )


async def test_call_recording_modules_are_not_loaded(client) -> None:
    """The clinic retains no audio. Recording must be off at the PBX too."""
    modules = await client.get_json("/asterisk/modules")
    loaded = {m["name"] for m in modules if m.get("status") == "Running"}

    assert "app_mixmonitor.so" not in loaded, "MixMonitor is loaded — recording is possible"
    assert "res_monitor.so" not in loaded, "res_monitor is loaded — recording is possible"


async def test_the_stasis_application_registers(client, settings) -> None:
    """The dialplan's Stasis() argument must match our app name.

    A mismatch is silent: calls reach Stasis, no application is listening, and
    Asterisk falls through the dialplan. The caller hears the fallback rather
    than the AI, and nothing in our logs says why.
    """
    await client.start()
    connected = await client.wait_connected(limit_seconds=5.0)
    assert connected, "the ARI event socket never connected"

    registered = {app["name"] for app in await client.get_json("/applications")}

    assert settings.asterisk_app_name in registered, (
        f"Stasis app {settings.asterisk_app_name!r} is not registered; "
        f"Asterisk knows about {sorted(registered)}"
    )
