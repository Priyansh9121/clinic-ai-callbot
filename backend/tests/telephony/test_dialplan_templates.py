"""The Asterisk templates must agree with each other and with the application.

These files are the only instructions a fresh setup gets, and nothing else
checks them: no test reads their contents, and a wrong value here fails at
call time on someone's PBX rather than in CI.

Two of these invariants have already been broken in this repository. The
softphone context drifted out of step with the dialplan, so every call —
including the proof-of-life test that involves no AI at all — was rejected
before any dialplan logic ran. And the dialplan's health check pointed at
loopback, which inside the VM is the VM, so every caller silently took the
reception fallback.

Deliberately free of markers and fixtures: this is text, and it must stay
runnable with no database and no PBX.
"""

from __future__ import annotations

import pathlib
import re

import pytest

from app.core.config import Settings

TEMPLATES = pathlib.Path(__file__).resolve().parents[3] / "infrastructure" / "asterisk"

#: Addresses that mean "this machine" and therefore mean the *PBX* when the
#: dialplan evaluates them. Correct only when the application shares a host
#: with Asterisk, which is not the topology these templates document.
LOOPBACK = ("127.0.0.1", "localhost", "::1")


def _read(name: str) -> str:
    path = TEMPLATES / name
    assert path.is_file(), f"{name} is missing from infrastructure/asterisk"
    return path.read_text()


def _live_lines(text: str) -> list[str]:
    """Lines with the Asterisk comments (`;`) stripped out.

    The trunk configuration is shipped commented out on purpose, and it names
    contexts that must *not* exist. Reading it as live configuration would
    produce a false failure.
    """
    return [line for line in text.splitlines() if not line.strip().startswith(";")]


@pytest.fixture(scope="module")
def defaults() -> Settings:
    """Application defaults, with any local .env deliberately excluded."""
    return Settings(_env_file=None)


# --- The regression that broke every call -----------------------------------
def test_every_endpoint_context_exists_in_the_dialplan() -> None:
    """A PJSIP `context=` naming no dialplan context rejects the call outright.

    Asterisk does not fall back to a default context. If these two files
    disagree, dialling 600 fails just as completely as dialling 700, which is
    what makes the symptom so confusing: the proof-of-life test that is
    supposed to isolate SIP problems fails for a dialplan reason.
    """
    endpoint_contexts = {
        match.group(1).strip()
        for line in _live_lines(_read("pjsip.conf.example"))
        if (match := re.match(r"\s*context\s*=\s*(\S+)", line))
    }
    defined = set(re.findall(r"^\[([^\]]+)\]", _read("extensions.conf.example"), re.MULTILINE))

    assert endpoint_contexts, "pjsip.conf.example declares no context at all"
    missing = endpoint_contexts - defined
    assert not missing, (
        f"pjsip.conf.example sends endpoints to {sorted(missing)}, which "
        f"extensions.conf.example does not define (it defines {sorted(defined)})"
    )


# --- Names the application and the dialplan must both know ------------------
def test_the_dialplan_hands_calls_to_the_configured_stasis_app(defaults: Settings) -> None:
    """A Stasis name mismatch is silent: the caller just gets the fallback."""
    apps = set(re.findall(r"Stasis\(([^)]+)\)", _read("extensions.conf.example")))

    assert apps == {defaults.asterisk_app_name}, (
        f"dialplan calls Stasis{sorted(apps)} but ASTERISK_APP_NAME defaults to "
        f"{defaults.asterisk_app_name!r}"
    )


def test_the_transfer_context_matches_the_one_the_application_dials(
    defaults: Settings,
) -> None:
    """`continue_in_dialplan` names this context; if it is absent, transfers fail."""
    defined = set(re.findall(r"^\[([^\]]+)\]", _read("extensions.conf.example"), re.MULTILINE))

    assert defaults.telephony_transfer_context in defined, (
        f"TELEPHONY_TRANSFER_CONTEXT defaults to "
        f"{defaults.telephony_transfer_context!r}, which the dialplan does not define"
    )


def test_the_transfer_destinations_exist_in_that_context(defaults: Settings) -> None:
    """The registry resolves to these extensions; nothing else can be reached."""
    text = _read("extensions.conf.example")
    # Anchored to a line of its own: the context is also named in the prose
    # header, and splitting on that would read the comment block instead.
    header = re.search(
        rf"^\[{re.escape(defaults.telephony_transfer_context)}\]\s*$", text, re.MULTILINE
    )
    assert header is not None, (
        f"extensions.conf.example defines no [{defaults.telephony_transfer_context}] section"
    )
    # Stop at the next context so we do not credit extensions defined elsewhere.
    context = re.split(r"^\[", text[header.end() :], maxsplit=1, flags=re.MULTILINE)[0]
    extensions = set(re.findall(r"^exten\s*=>\s*([^,]+),", context, re.MULTILINE))

    for name, wanted in (
        ("RECEPTION_EXTENSION", defaults.reception_extension),
        ("VOICEMAIL_EXTENSION", defaults.voicemail_extension),
    ):
        assert wanted in extensions, (
            f"{name} defaults to {wanted!r}, which [{defaults.telephony_transfer_context}] "
            f"does not define (it defines {sorted(extensions)})"
        )


# --- Addresses that must survive crossing a host boundary -------------------
def test_the_dialplan_never_points_at_loopback() -> None:
    """Inside the VM, loopback is the VM.

    The health check then fails for every call and the caller is routed to
    reception, having never reached the AI — with nothing in any log saying
    the address was the problem. The media host has the same failure mode,
    except the call connects first and the caller hears silence.
    """
    globals_block = _read("extensions.conf.example").split("[ai-internal]", 1)[0]

    for line in _live_lines(globals_block):
        if "=" not in line:
            continue
        name, _, value = line.partition("=")
        assert not any(host in value for host in LOOPBACK), (
            f"{name.strip()} is set to a loopback address. Asterisk evaluates it, "
            f"so it must name the machine running the clinic AI as the PBX sees it."
        )


def test_the_templates_carry_no_real_addresses_or_secrets() -> None:
    """Machine addresses vary per setup, so they belong in placeholders.

    A committed IP is not a security problem, but it is a correctness one: it
    reads as configuration that has been checked, and the next person keeps it.
    """
    for name in ("extensions.conf.example", "http.conf.example", "pjsip.conf.example"):
        for line in _live_lines(_read(name)):
            assert not re.search(r"\b192\.168\.\d+\.\d+\b", line), (
                f"{name} hard-codes a machine address: {line.strip()!r}. "
                f"Use a ${{PLACEHOLDER}} and document the validated value in a comment."
            )


def test_the_ari_credential_is_a_placeholder() -> None:
    """The password must be substituted at copy time, never committed."""
    text = _read("ari.conf.example")
    password = re.search(r"^password\s*=\s*(.+)$", text, re.MULTILINE)

    assert password is not None, "ari.conf.example defines no password line"
    assert password.group(1).strip().startswith("${"), (
        "ari.conf.example must ship a ${...} placeholder, not a real credential"
    )
