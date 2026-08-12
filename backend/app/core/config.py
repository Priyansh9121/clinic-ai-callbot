"""Application configuration.

All runtime configuration is read from the environment (or a local .env file).
Nothing in this module may contain a production secret — defaults are chosen so
that a missing value fails loudly in production rather than silently running
insecurely.
"""

from __future__ import annotations

import sys
from functools import lru_cache
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# repo_root/backend/app/core/config.py -> repo_root
REPO_ROOT = Path(__file__).resolve().parents[3]

AppEnv = Literal["development", "test", "staging", "production"]
Role = Literal["admin", "staff", "readonly"]

# A development default that must never survive into production. The validator
# below rejects it outright when APP_ENV=production.
INSECURE_DEV_SECRET = "dev-insecure-secret-change-me-0000000000000000"  # noqa: S105


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(REPO_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Application -------------------------------------------------------
    app_env: AppEnv = "development"
    app_name: str = "clinic-ai-callbot"
    app_debug: bool = False
    app_host: str = "127.0.0.1"
    app_port: int = 8000
    log_level: str = "INFO"
    log_format: Literal["console", "json"] = "console"
    app_secret_key: SecretStr = SecretStr(INSECURE_DEV_SECRET)

    # --- Database ----------------------------------------------------------
    database_url: str = "postgresql+asyncpg://clinic_app:clinic_app@127.0.0.1:5433/clinic_callbot"
    database_pool_size: int = 10
    database_max_overflow: int = 5
    database_echo: bool = False

    # --- Redis -------------------------------------------------------------
    redis_url: str = ""
    session_ttl_seconds: int = 1800

    # --- Clinic defaults ---------------------------------------------------
    clinic_timezone: str = "Australia/Melbourne"
    clinic_country: str = "AU"
    emergency_number: str = "000"
    reception_phone: str = "+61390000000"

    # --- Conversation ------------------------------------------------------
    max_misunderstandings: int = 2
    max_slot_offers: int = 3
    availability_search_days: int = 14
    booking_lead_minutes: int = 60

    # --- Silence handling (voice pipeline, Milestone 3) --------------------
    silence_short_seconds: float = 1.2
    silence_checkin_seconds: float = 4.0
    silence_reprompt_seconds: float = 8.0
    silence_terminate_seconds: float = 15.0

    # --- Privacy -----------------------------------------------------------
    retain_transcripts: bool = False
    retain_audio: bool = False
    call_data_retention_days: int = 90

    # --- Security ----------------------------------------------------------
    cors_allowed_origins: str = ""
    staff_api_keys: str = ""
    rate_limit_enabled: bool = True
    rate_limit_requests: int = 120
    rate_limit_window_seconds: int = 60

    # --- Patient verification ---------------------------------------------
    otp_length: int = 6
    otp_ttl_seconds: int = 300
    otp_max_attempts: int = 3

    # --- Later-milestone providers (declared, not yet wired) ---------------
    llm_provider: Literal["none", "ollama", "llamacpp"] = "none"
    llm_base_url: str = "http://127.0.0.1:11434"
    llm_model: str = "qwen2.5:3b-instruct"
    llm_timeout_seconds: float = 6.0
    llm_min_confidence: float = 0.6

    stt_provider: Literal["none", "faster_whisper", "whispercpp"] = "none"
    stt_model: str = "small.en"
    tts_provider: Literal["none", "piper"] = "none"
    tts_voice: str = "en_AU-lessac-medium"
    vad_provider: Literal["none", "silero", "webrtc"] = "none"

    asterisk_ari_url: str = "http://127.0.0.1:8088/ari"
    asterisk_ari_user: str = ""
    asterisk_ari_password: SecretStr = SecretStr("")

    # --- Derived helpers ---------------------------------------------------
    @property
    def is_production(self) -> bool:
        return self.app_env == "production"

    @property
    def tz(self) -> ZoneInfo:
        """Default clinic timezone. Per-clinic values come from the database."""
        return ZoneInfo(self.clinic_timezone)

    @property
    def cors_origins(self) -> list[str]:
        return [o.strip() for o in self.cors_allowed_origins.split(",") if o.strip()]

    @property
    def staff_api_key_map(self) -> dict[str, Role]:
        """Parse ``key:role`` pairs. Development-only auth mechanism."""
        mapping: dict[str, Role] = {}
        for entry in self.staff_api_keys.split(","):
            entry = entry.strip()
            if not entry or ":" not in entry:
                continue
            key, _, role = entry.partition(":")
            role = role.strip()
            if role in ("admin", "staff", "readonly") and key.strip():
                mapping[key.strip()] = role  # type: ignore[assignment]
        return mapping

    @property
    def session_store_backend(self) -> Literal["redis", "memory"]:
        return "redis" if self.redis_url else "memory"

    # --- Validation --------------------------------------------------------
    @field_validator("clinic_timezone")
    @classmethod
    def _validate_tz(cls, v: str) -> str:
        try:
            ZoneInfo(v)
        except ZoneInfoNotFoundError as exc:  # pragma: no cover - config error path
            raise ValueError(f"Unknown timezone: {v!r}") from exc
        return v

    @field_validator("database_url")
    @classmethod
    def _validate_db_url(cls, v: str) -> str:
        if not v.startswith("postgresql+asyncpg://"):
            raise ValueError(
                "DATABASE_URL must use the asyncpg driver "
                "(postgresql+asyncpg://user:pass@host:port/db)"
            )
        return v

    @model_validator(mode="after")
    def _production_hardening(self) -> Settings:
        """Refuse to boot a production instance with development defaults."""
        if not self.is_production:
            return self

        problems: list[str] = []
        secret = self.app_secret_key.get_secret_value()
        if secret == INSECURE_DEV_SECRET or len(secret) < 32:
            problems.append("APP_SECRET_KEY must be set to a unique value of >= 32 characters")
        if self.app_debug:
            problems.append("APP_DEBUG must be false in production")
        if not self.redis_url:
            problems.append("REDIS_URL is required in production (in-memory sessions are not safe)")
        if self.retain_audio:
            problems.append(
                "RETAIN_AUDIO must be false unless enabled through privacy governance review"
            )
        if "*" in self.cors_origins:
            problems.append("CORS_ALLOWED_ORIGINS must not be a wildcard in production")
        if any(k.startswith("dev-") for k in self.staff_api_key_map):
            problems.append("Development STAFF_API_KEYS must not be used in production")
        if problems:
            raise ValueError("Insecure production configuration:\n  - " + "\n  - ".join(problems))
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached settings accessor. Used as a FastAPI dependency and in services."""
    try:
        return Settings()
    except Exception as exc:  # pragma: no cover - startup failure path
        print(f"FATAL: invalid configuration: {exc}", file=sys.stderr)
        raise
