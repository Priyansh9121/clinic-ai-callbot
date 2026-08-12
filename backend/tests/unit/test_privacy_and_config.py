"""Log redaction, configuration hardening and session storage."""

from __future__ import annotations

import pytest

from app.conversation.session import ConversationSession
from app.conversation.states import ConversationState
from app.conversation.store import InMemorySessionStore
from app.core.config import INSECURE_DEV_SECRET, Settings
from app.core.logging import redact_processor, scrub_text
from app.core.metrics import MetricsRegistry
from app.patients.service import mask_mobile, normalise_mobile

VALID_DSN = "postgresql+asyncpg://u:p@localhost:5432/db"


# --- Log redaction ----------------------------------------------------------
def test_sensitive_keys_are_removed_from_log_records() -> None:
    event = {
        "event": "booking",
        "first_name": "Margaret",
        "last_name": "Hughes",
        "date_of_birth": "1958-03-14",
        "mobile": "0491570156",
        "email": "m@example.invalid",
        "password": "hunter2",
        "otp_code": "123456",
        "patient_mobile": "0491570156",
        "appointment_id": "abc-123",
    }
    result = redact_processor(None, "info", dict(event))

    for key in (
        "first_name",
        "last_name",
        "date_of_birth",
        "mobile",
        "email",
        "password",
        "otp_code",
        "patient_mobile",
    ):
        assert result[key] == "[redacted]", key
    # Non-sensitive fields survive.
    assert result["appointment_id"] == "abc-123"


def test_identifiers_inside_free_text_are_scrubbed() -> None:
    assert "0491570156" not in scrub_text("call me on 0491570156")
    assert "@" not in scrub_text("email me at person@example.com")
    assert "1958-03-14" not in scrub_text("born 1958-03-14")


def test_nested_dictionaries_are_redacted() -> None:
    result = redact_processor(None, "info", {"event": "x", "ctx": {"mobile": "0491570156"}})
    assert result["ctx"]["mobile"] == "[redacted]"


# --- Configuration ----------------------------------------------------------
def test_development_defaults_are_permitted() -> None:
    # Values are passed explicitly rather than inherited from the developer's
    # local .env, which Settings would otherwise read.
    settings = Settings(app_env="development", database_url=VALID_DSN, redis_url="")
    assert settings.is_production is False
    assert settings.session_store_backend == "memory"


def test_redis_is_used_when_configured() -> None:
    settings = Settings(database_url=VALID_DSN, redis_url="redis://localhost:6379/0")
    assert settings.session_store_backend == "redis"


def test_production_rejects_the_development_secret() -> None:
    with pytest.raises(ValueError, match="APP_SECRET_KEY"):
        Settings(
            app_env="production",
            database_url=VALID_DSN,
            app_secret_key=INSECURE_DEV_SECRET,
            redis_url="redis://localhost:6379/0",
        )


def test_production_requires_redis() -> None:
    """In-memory sessions silently break across workers, so they are refused."""
    with pytest.raises(ValueError, match="REDIS_URL"):
        Settings(
            app_env="production",
            database_url=VALID_DSN,
            app_secret_key="a" * 40,
            redis_url="",
        )


def test_production_rejects_debug_and_wildcard_cors() -> None:
    with pytest.raises(ValueError, match="APP_DEBUG"):
        Settings(
            app_env="production",
            database_url=VALID_DSN,
            app_secret_key="a" * 40,
            redis_url="redis://localhost:6379/0",
            app_debug=True,
        )
    with pytest.raises(ValueError, match="CORS"):
        Settings(
            app_env="production",
            database_url=VALID_DSN,
            app_secret_key="a" * 40,
            redis_url="redis://localhost:6379/0",
            cors_allowed_origins="*",
        )


def test_production_rejects_development_api_keys() -> None:
    with pytest.raises(ValueError, match="STAFF_API_KEYS"):
        Settings(
            app_env="production",
            database_url=VALID_DSN,
            app_secret_key="a" * 40,
            redis_url="redis://localhost:6379/0",
            staff_api_keys="dev-admin-key:admin",
        )


def test_production_rejects_audio_retention() -> None:
    with pytest.raises(ValueError, match="RETAIN_AUDIO"):
        Settings(
            app_env="production",
            database_url=VALID_DSN,
            app_secret_key="a" * 40,
            redis_url="redis://localhost:6379/0",
            retain_audio=True,
        )


def test_a_non_asyncpg_dsn_is_rejected() -> None:
    with pytest.raises(ValueError, match="asyncpg"):
        Settings(database_url="postgresql://u:p@localhost/db")


def test_unknown_timezone_is_rejected() -> None:
    with pytest.raises(ValueError, match=r"[Tt]imezone|Unknown"):
        Settings(database_url=VALID_DSN, clinic_timezone="Mars/Olympus_Mons")


def test_api_keys_parse_into_roles() -> None:
    settings = Settings(
        database_url=VALID_DSN,
        staff_api_keys="k1:admin, k2:staff ,k3:readonly,bad-entry,k4:wrongrole",
    )
    assert settings.staff_api_key_map == {"k1": "admin", "k2": "staff", "k3": "readonly"}


def test_secret_is_not_printed_in_the_repr() -> None:
    settings = Settings(database_url=VALID_DSN, app_secret_key="super-secret-value")
    assert "super-secret-value" not in repr(settings)


# --- Session store ----------------------------------------------------------
async def test_session_round_trips_through_the_store() -> None:
    import uuid

    store = InMemorySessionStore(ttl_seconds=60)
    session = ConversationSession(session_id="sess_abc123", clinic_id=uuid.uuid4())
    session.state = ConversationState.SELECT_SLOT
    session.booking.practitioner_any = True

    await store.save(session)
    loaded = await store.get("sess_abc123")

    assert loaded is not None
    assert loaded.state is ConversationState.SELECT_SLOT
    assert loaded.booking.practitioner_any is True
    assert loaded.clinic_id == session.clinic_id


async def test_expired_sessions_are_dropped() -> None:
    import uuid

    store = InMemorySessionStore(ttl_seconds=0)
    session = ConversationSession(session_id="sess_expire", clinic_id=uuid.uuid4())
    await store.save(session)
    assert await store.get("sess_expire") is None


async def test_deleting_a_session() -> None:
    import uuid

    store = InMemorySessionStore(ttl_seconds=60)
    await store.save(ConversationSession(session_id="sess_del01", clinic_id=uuid.uuid4()))
    await store.delete("sess_del01")
    assert await store.get("sess_del01") is None


def test_memory_snapshot_contains_no_identifiers() -> None:
    import uuid

    session = ConversationSession(session_id="sess_snap01", clinic_id=uuid.uuid4())
    session.patient_first_name = "Margaret"
    session.identity_last_name = "Hughes"
    snapshot = str(session.memory_snapshot()).lower()
    assert "margaret" not in snapshot
    assert "hughes" not in snapshot


# --- Mobile handling --------------------------------------------------------
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("0491570156", "+61491570156"),
        ("0491 570 156", "+61491570156"),
        ("+61491570156", "+61491570156"),
        ("61491570156", "+61491570156"),
        ("0391570156", None),  # landline, not a mobile
        ("not a number", None),
        (None, None),
    ],
)
def test_mobile_normalisation(raw: str | None, expected: str | None) -> None:
    assert normalise_mobile(raw) == expected


def test_masked_mobile_reveals_only_the_last_three_digits() -> None:
    masked = mask_mobile("+61491570156")
    assert masked is not None
    assert masked.endswith("156")
    assert "491570" not in masked


# --- Metrics ----------------------------------------------------------------
def test_metrics_record_counters_and_latency() -> None:
    registry = MetricsRegistry()
    registry.increment("calls", labels={"outcome": "BOOKED"})
    registry.increment("calls", labels={"outcome": "BOOKED"})
    registry.observe("latency", 120.0)
    registry.observe("latency", 480.0)

    snapshot = registry.snapshot()
    assert snapshot["counters"]["calls{outcome=BOOKED}"] == 2
    latency = snapshot["latency_ms"]["latency"]
    assert latency["count"] == 2
    assert latency["avg_ms"] == 300.0


def test_timer_records_even_when_the_block_raises() -> None:
    registry = MetricsRegistry()
    with pytest.raises(RuntimeError), registry.timer("op"):
        raise RuntimeError("boom")
    assert registry.snapshot()["latency_ms"]["op"]["count"] == 1
