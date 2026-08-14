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

from app.core.config import get_settings, is_placeholder
from app.telephony.ari import AriError
from app.telephony.factory import ari_client

pytestmark = pytest.mark.telephony_live


@pytest.fixture(scope="module")
def settings():
    """The real configuration, skipped when it was never filled in.

    Placeholders are "not configured" rather than "wrong password", so the
    suite skips instead of failing. `is_placeholder` is shared with the
    application so the two cannot drift: `telephony_configured` declines on
    exactly the same values these tests skip on.
    """
    config = get_settings()
    credential = config.asterisk_ari_password.get_secret_value()
    if not config.asterisk_ari_user or is_placeholder(config.asterisk_ari_user):
        pytest.skip("ASTERISK_ARI_USER is unset or a placeholder")
    if not credential or is_placeholder(credential):
        pytest.skip("ASTERISK_ARI_PASSWORD is unset or a placeholder — substitute it in .env")
    return config


def _is_auth_failure(exc: AriError) -> bool:
    """Whether Asterisk answered and refused the credential."""
    return "401" in str(exc) or "403" in str(exc)


@pytest.fixture
async def client(settings):
    ari = ari_client(settings)
    try:
        await ari.asterisk_info()
    except AriError as exc:
        # A rejected credential must NOT skip. Asterisk answered — that is a
        # misconfiguration these tests exist to catch, and skipping it reports
        # a live, wrongly-configured PBX as "no PBX here", which is how a 401
        # survives a green run. Only a genuinely absent PBX is a skip.
        if not _is_auth_failure(exc):
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
