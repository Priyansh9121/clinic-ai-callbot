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

# Spellings of "every interface here" and of the unspecified address. None of
# them is a destination another machine can dial, so none may be handed to
# Asterisk as one.
_UNROUTABLE_HOSTS = frozenset({"", "0.0.0.0", "::", "0:0:0:0:0:0:0:0", "0", "*"})  # noqa: S104

#: Values shipped in .env.example to be substituted at setup. They are not
#: credentials, and treating them as ones costs an afternoon: the PBX answers
#: every request with 401, which reads as a wrong password rather than as a
#: password that was never set.
PLACEHOLDER_SECRETS = frozenset({"CHANGE_ME", "change-me", "changeme", "change_me"})


def is_placeholder(secret: str) -> bool:
    """Whether *secret* is an unsubstituted template value rather than a secret."""
    return secret.strip() in PLACEHOLDER_SECRETS


def _is_routable_host(host: str) -> bool:
    """Whether *host* is an address another machine can actually connect to.

    Compared after trimming whitespace and any IPv6 brackets, because the
    failure this prevents is silent: a wildcard reaches Asterisk as a dial
    string, the call connects, and the caller hears nothing. A trailing space
    is not a meaningful difference and must not buy a pass.
    """
    return host.strip().strip("[]").lower() not in _UNROUTABLE_HOSTS


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

    # --- Local LLM (Milestone 2) -------------------------------------------
    llm_provider: Literal["none", "ollama", "llamacpp"] = "none"
    # baseline: deterministic analyser only (Milestone 1 behaviour).
    # llm:      prefer the model, fall back to the baseline on any failure.
    # shadow:   baseline is authoritative; the model runs alongside for
    #           comparison metrics only and cannot affect the caller.
    llm_mode: Literal["baseline", "llm", "shadow"] = "baseline"
    llm_base_url: str = "http://127.0.0.1:11434"
    llm_model: str = ""
    # A caller is on the phone. Anything slower than this is worse than a
    # slightly less accurate deterministic answer, so the request is cancelled.
    llm_timeout_seconds: float = 3.0
    # Below this, the model's own reported confidence is not trusted and the
    # deterministic result is used instead. Note that small instruct models are
    # poorly calibrated and tend to report high confidence regardless; this is
    # a safety valve, not a probability. See docs/architecture/llm.md.
    llm_min_confidence: float = 0.6
    # Bounds simultaneous generations so concurrent calls cannot exhaust the
    # machine. Saturation degrades to the baseline rather than queueing.
    llm_max_concurrency: int = 2
    # Structured classification needs very few tokens; capping output bounds
    # both latency and the size of anything that has to be validated.
    llm_max_output_tokens: int = 200
    llm_temperature: float = 0.0
    # Consecutive provider failures before the breaker opens and every turn
    # goes straight to the baseline until the cooldown elapses.
    llm_failure_threshold: int = 3
    llm_recovery_seconds: float = 30.0
    # Skip the model when the deterministic analyser is already certain
    # ("yes", "the first one", an explicit request for a human). Pure latency
    # win; set false to force every turn through the model when benchmarking.
    llm_hybrid_routing: bool = True
    # How long Ollama keeps the model resident. The default (5 minutes)
    # lets an idle clinic line pay a cold reload on the next call.
    llm_keep_alive: str = "30m"

    # --- Voice pipeline (Milestone 3) --------------------------------------
    # All default to "none": the text deployment must not need audio libraries,
    # models or a sound card. See docs/architecture/voice-pipeline.md.
    stt_provider: Literal["none", "faster_whisper"] = "none"
    # base.en measured as the best accuracy/latency balance on this hardware;
    # tiny.en is faster but garbles ordinary speech, small.en is ~3x slower.
    stt_model: str = "base.en"
    stt_language: str = "en"
    stt_compute_type: str = "int8"
    stt_beam_size: int = 1
    # Bias decoding toward practitioner and appointment-type names. Clinic
    # vocabulary only — never patient data.
    stt_vocabulary_bias: bool = True

    tts_provider: Literal["none", "piper"] = "none"
    # Path to a Piper .onnx voice. Piper ships no en_AU voice, so en_GB is the
    # closest available for an Australian clinic. `alan` was chosen over `alba`
    # on measured intelligibility, not preference: alba is a Scottish voice and
    # rendered "away" as "a wee" and a street address as "RAT-40, 2-RIVICIDE
    # DRIVE". scripts/benchmark_tts.py reproduces both.
    tts_voice: str = "models/piper/en_GB-alan-medium.onnx"
    # <1.0 speaks faster. None keeps the voice's own pacing.
    tts_length_scale: float | None = None
    tts_cache_entries: int = 64

    vad_provider: Literal["none", "silero", "energy"] = "none"
    vad_model: str = "models/silero/silero_vad.onnx"
    vad_threshold: float = 0.5

    # --- Endpointing -------------------------------------------------------
    # Silence after speech that ends a turn. 500 ms measured as the balance
    # between cutting people off and feeling sluggish; see the voice benchmark.
    voice_end_silence_ms: int = 500
    # How long to wait for the caller to say anything at all.
    voice_initial_silence_ms: int = 6_000
    # Shorter bursts are a cough or a door, not a turn.
    voice_min_speech_ms: int = 200
    # Hard cap so a stuck line cannot buffer minutes of audio.
    voice_max_utterance_ms: int = 20_000

    # --- Voice behaviour ---------------------------------------------------
    # Speak a short acknowledgement if the caller has heard nothing this long
    # after they stopped talking. Endpointing and transcription come out of the
    # same budget, so this is not "how long the engine gets".
    voice_acknowledgement_after_ms: int = 900
    voice_acknowledgement_enabled: bool = True
    # Consecutive speech frames required to treat speech-over-agent as a real
    # barge-in. Raise it if speaker bleed causes false interruptions.
    voice_barge_in_frames: int = 1
    # Audio devices: None uses the system default.
    voice_input_device: str = ""
    voice_output_device: str = ""

    # --- Telephony (Milestone 4) -------------------------------------------
    # Off unless an ARI user is configured, so the text and local-voice
    # deployments need no PBX. See docs/architecture/telephony.md.
    telephony_enabled: bool = False
    asterisk_ari_url: str = "http://127.0.0.1:8088/ari"
    asterisk_ari_user: str = ""
    asterisk_ari_password: SecretStr = SecretStr("")
    #: Stasis application name. Must match the dialplan's Stasis() argument.
    asterisk_app_name: str = "clinic-ai"

    # AudioSocket media server. Loopback by default: the protocol carries no
    # credential, so anything that can reach this port can inject audio into a
    # live call. Across hosts it belongs on a private media network.
    #
    # Two addresses, because they answer different questions. Asterisk connects
    # *to* us, so when it runs in a VM or on another host the socket must bind
    # somewhere reachable (0.0.0.0) while Asterisk must be told a routable
    # address (the host's IP on that network). Telling Asterisk "0.0.0.0" is
    # not a destination, and "127.0.0.1" means the PBX itself — which is how a
    # call connects and then hears nothing at all.
    telephony_media_host: str = "127.0.0.1"
    telephony_media_port: int = 8090
    #: Address Asterisk dials for media. Empty means "same as the bind host",
    #: which is correct only when the PBX and this process share a host.
    telephony_media_advertise_host: str = ""
    #: Wire format of the media leg: slin | slin16 | pcmu | pcma. slin16 avoids
    #: resampling entirely where the dialplan can raise the channel format.
    telephony_wire_format: Literal["slin", "slin16", "pcmu", "pcma"] = "slin"
    #: soxr quality for 8 kHz <-> 16 kHz conversion. Lower settings have much
    #: less filter delay; see the telephone-band benchmark before changing.
    telephony_resample_quality: str = "HQ"

    # Transfer. Extensions are validated as bare dialplan extensions at load,
    # and the context is transfer-only — no outbound trunk is reachable from it.
    telephony_transfer_context: str = "clinic-transfer"
    reception_extension: str = "100"
    voicemail_extension: str = "101"

    #: Concurrent calls this machine will accept. Local STT and LLM inference
    #: are the real limit, not the transport — measure before raising it.
    telephony_max_active_calls: int = 2
    #: What happens at capacity: reject (dialplan failover routes to reception)
    #: or transfer_reception. A clinic policy decision, not a code default.
    telephony_capacity_policy: Literal["reject", "transfer_reception"] = "reject"
    #: Drain before releasing the channel, so a goodbye is not clipped.
    telephony_hangup_drain_ms: int = 250
    #: Salt for the caller-number fingerprint. An unsalted hash of a 10-digit
    #: number is a lookup table, not an anonymisation. Set per deployment.
    telephony_caller_salt: SecretStr = SecretStr("change-me-per-deployment")

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

    @property
    def voice_configured(self) -> bool:
        """Whether a full voice pipeline is available.

        All three providers are required: STT with no TTS could hear but not
        answer, and either without VAD could not tell when a turn ended.
        """
        return (
            self.stt_provider != "none"
            and self.tts_provider != "none"
            and self.vad_provider != "none"
        )

    @property
    def media_advertise_host(self) -> str:
        """The address Asterisk should connect to for media.

        Falls back to the bind host, which is right for an all-on-one-host
        setup. A wildcard bind has no such fallback: there is no way to guess
        which interface the PBX can reach, so it must be configured.
        """
        if self.telephony_media_advertise_host:
            return self.telephony_media_advertise_host
        return self.telephony_media_host

    @property
    def media_advertise_is_routable(self) -> bool:
        """Whether the advertised address is something Asterisk can dial."""
        return _is_routable_host(self.media_advertise_host)

    @property
    def health_advertise_url(self) -> str:
        """The ``/health/live`` URL the dialplan should be pointed at.

        Asterisk reaches this process over the same network it reaches the
        media listener on — same machine, same interface — so the media
        advertise host answers this question too and no second setting is
        needed to describe the topology.

        What no setting can know is which address uvicorn was actually bound
        to. ``--host 127.0.0.1`` is unreachable from the PBX however this
        reads, which is why the status command prints it for comparison
        rather than claiming it works.
        """
        return f"http://{self.media_advertise_host}:{self.app_port}/health/live"

    @property
    def telephony_configured(self) -> bool:
        """Whether inbound calls can actually be served.

        Telephony needs the voice pipeline *and* a PBX to talk to. A partial
        configuration answering calls it cannot converse on would be worse than
        not answering, because the dialplan's failover to reception only fires
        when we decline.

        An unsubstituted placeholder counts as unconfigured, not as a wrong
        password. It cannot authenticate, so starting on one buys nothing but a
        permanent 401 reconnect loop and a ``/health`` that says ``down`` when
        the truth is ``never set up``.
        """
        credential = self.asterisk_ari_password.get_secret_value()
        return (
            self.telephony_enabled
            and self.voice_configured
            and bool(self.asterisk_ari_user)
            and not is_placeholder(self.asterisk_ari_user)
            and bool(credential)
            and not is_placeholder(credential)
        )

    def device_or_default(self, configured: str) -> int | str | None:
        """Resolve a configured audio device, falling back to the system default."""
        value = configured.strip()
        if not value:
            return None
        return int(value) if value.isdigit() else value

    @property
    def llm_configured(self) -> bool:
        """A provider and a model are both named. Says nothing about reachability."""
        return self.llm_provider != "none" and bool(self.llm_model.strip())

    @property
    def effective_llm_mode(self) -> Literal["baseline", "llm", "shadow"]:
        """The mode that will actually run.

        Requesting ``llm`` or ``shadow`` without a configured provider is a
        configuration mistake, not a reason to refuse to answer the phone: it
        degrades to ``baseline`` and the factory logs the discrepancy. The
        clinic stays reachable, which is the overriding requirement.
        """
        if not self.llm_configured:
            return "baseline"
        return self.llm_mode

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


def _configuration_problem(exc: Exception) -> str:
    """Name the settings that are wrong, without echoing what they contain.

    A pydantic ValidationError renders the offending input, and the settings
    most likely to be malformed are the ones carrying credentials: a bad
    DATABASE_URL prints the password to stderr, where startup output is
    routinely pasted into issues and chat. Report the field and the reason;
    the operator can look up the value themselves.
    """
    errors = getattr(exc, "errors", None)
    if not callable(errors):
        return type(exc).__name__
    try:
        return "; ".join(
            f"{'.'.join(str(p) for p in item.get('loc', ())) or '(root)'}: {item.get('msg', '')}"
            for item in errors()
        )
    except Exception:  # pragma: no cover - defensive
        return type(exc).__name__


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached settings accessor. Used as a FastAPI dependency and in services."""
    try:
        return Settings()
    except Exception as exc:  # pragma: no cover - startup failure path
        print(f"FATAL: invalid configuration: {_configuration_problem(exc)}", file=sys.stderr)
        raise
