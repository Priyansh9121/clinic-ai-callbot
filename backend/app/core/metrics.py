"""Lightweight in-process metrics.

Deliberately dependency-free: counters and latency histograms held in memory
and exposed via ``GET /health/metrics``. A Prometheus exporter can be layered
on later without changing call sites (see docs/architecture/observability.md).

Privacy: label values are restricted to a small closed set of enum-like
strings. Never pass patient identifiers, free text or phone numbers as labels.
"""

from __future__ import annotations

import time
from collections import defaultdict
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from threading import Lock

# Latency buckets in milliseconds, chosen around the conversational budget:
# anything over ~1500 ms of dead air is perceptible to a caller.
DEFAULT_BUCKETS_MS: tuple[float, ...] = (25, 50, 100, 200, 400, 800, 1500, 3000, 6000)


@dataclass
class Histogram:
    buckets_ms: tuple[float, ...] = DEFAULT_BUCKETS_MS
    counts: list[int] = field(default_factory=list)
    total_ms: float = 0.0
    count: int = 0
    max_ms: float = 0.0

    def __post_init__(self) -> None:
        if not self.counts:
            self.counts = [0] * (len(self.buckets_ms) + 1)

    def observe(self, value_ms: float) -> None:
        self.count += 1
        self.total_ms += value_ms
        self.max_ms = max(self.max_ms, value_ms)
        for i, edge in enumerate(self.buckets_ms):
            if value_ms <= edge:
                self.counts[i] += 1
                return
        self.counts[-1] += 1

    def snapshot(self) -> dict[str, float | int | dict[str, int]]:
        labels = [f"<={int(b)}ms" for b in self.buckets_ms] + ["+Inf"]
        return {
            "count": self.count,
            "avg_ms": round(self.total_ms / self.count, 2) if self.count else 0.0,
            "max_ms": round(self.max_ms, 2),
            "buckets": dict(zip(labels, self.counts, strict=True)),
        }


class MetricsRegistry:
    """Thread-safe registry. One process-wide instance is used by default."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._counters: dict[str, int] = defaultdict(int)
        self._histograms: dict[str, Histogram] = {}
        self._gauges: dict[str, float] = {}

    @staticmethod
    def _key(name: str, labels: dict[str, str] | None) -> str:
        if not labels:
            return name
        rendered = ",".join(f"{k}={v}" for k, v in sorted(labels.items()))
        return f"{name}{{{rendered}}}"

    def increment(self, name: str, *, labels: dict[str, str] | None = None, by: int = 1) -> None:
        with self._lock:
            self._counters[self._key(name, labels)] += by

    def gauge(self, name: str, value: float, *, labels: dict[str, str] | None = None) -> None:
        with self._lock:
            self._gauges[self._key(name, labels)] = value

    def observe(self, name: str, value_ms: float, *, labels: dict[str, str] | None = None) -> None:
        key = self._key(name, labels)
        with self._lock:
            hist = self._histograms.get(key)
            if hist is None:
                hist = self._histograms[key] = Histogram()
            hist.observe(value_ms)

    @contextmanager
    def timer(self, name: str, *, labels: dict[str, str] | None = None) -> Iterator[None]:
        """Time a block and record it, including when it raises."""
        started = time.perf_counter()
        try:
            yield
        finally:
            self.observe(name, (time.perf_counter() - started) * 1000.0, labels=labels)

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            return {
                "counters": dict(self._counters),
                "gauges": dict(self._gauges),
                "latency_ms": {k: v.snapshot() for k, v in self._histograms.items()},
            }

    def reset(self) -> None:
        with self._lock:
            self._counters.clear()
            self._histograms.clear()
            self._gauges.clear()


metrics = MetricsRegistry()


# --- Canonical metric names -------------------------------------------------
# Referenced by name everywhere so the dashboard and tests cannot drift.
class M:
    CONVERSATION_TURN = "conversation.turn_latency"
    CONVERSATION_TURNS_TOTAL = "conversation.turns_total"
    INTENT_DETECTED = "conversation.intent_detected"
    INTENT_CONFIDENCE = "conversation.intent_confidence_pct"
    MISUNDERSTANDING = "conversation.misunderstanding"
    HUMAN_TRANSFER = "conversation.human_transfer"
    EMERGENCY_TRIGGERED = "safety.emergency_triggered"
    SAFETY_ESCALATION = "safety.escalation"

    DB_QUERY = "db.query_latency"
    AVAILABILITY_SEARCH = "appointments.availability_search_latency"
    BOOKING_SUCCESS = "appointments.booking_success"
    BOOKING_FAILURE = "appointments.booking_failure"
    BOOKING_LATENCY = "appointments.booking_latency"
    CANCELLATION = "appointments.cancellation"
    RESCHEDULE = "appointments.reschedule"

    CALL_DURATION = "call.duration_ms"
    CALL_TURNS = "call.turns"

    # --- Local LLM (Milestone 2) -------------------------------------------
    # Label values are closed sets only (provider, model, state, reason code).
    # Never caller text, names or identifiers.
    LLM_LATENCY = "llm.analyse_latency"
    LLM_REQUESTS = "llm.requests_total"
    LLM_SUCCESS = "llm.success"
    LLM_FAILURE = "llm.failure"
    LLM_TIMEOUT = "llm.timeout"
    LLM_INVALID_RESPONSE = "llm.invalid_response"
    LLM_LOW_CONFIDENCE = "llm.low_confidence"
    LLM_FALLBACK = "llm.fallback"
    LLM_SKIPPED = "llm.skipped"
    LLM_CIRCUIT_OPEN = "llm.circuit_open"
    LLM_SATURATED = "llm.saturated"
    LLM_SHADOW_AGREEMENT = "llm.shadow_agreement"
    LLM_SHADOW_DISAGREEMENT = "llm.shadow_disagreement"
    ANALYSER_LATENCY = "llm.analyser_total_latency"

    # --- Voice pipeline (Milestone 3) --------------------------------------
    # Label values are closed sets only (provider, model, reason code, voice
    # state). Never a transcript, a phrase, or anything caller-specific.
    STT_LATENCY = "speech.stt_latency"
    STT_REAL_TIME_FACTOR = "speech.stt_rtf_pct"
    STT_EMPTY = "speech.stt_empty"
    STT_HALLUCINATION_DISCARDED = "speech.stt_hallucination_discarded"

    TTS_TOTAL_LATENCY = "speech.tts_total_latency"
    TTS_CACHE_HIT = "speech.tts_cache_hit"
    TTS_CACHE_MISS = "speech.tts_cache_miss"

    VAD_SPEECH_START_LATENCY = "speech.vad_speech_start_latency"
    VAD_ENDPOINT_LATENCY = "speech.vad_endpoint_latency"

    VOICE_SESSIONS = "voice.sessions"
    VOICE_TURNS = "voice.turns"
    VOICE_TIME_TO_FIRST_AUDIO = "voice.time_to_first_audio"
    VOICE_BARGE_IN_LATENCY = "voice.barge_in_latency"
    VOICE_TURN_CANCELLED = "voice.turn_cancelled"
    VOICE_STALE_RESPONSE_DISCARDED = "voice.stale_response_discarded"
    VOICE_ACKNOWLEDGEMENT_PLAYED = "voice.acknowledgement_played"
    VOICE_NO_SPEECH = "voice.no_speech"
    VOICE_UTTERANCE_TOO_LONG = "voice.utterance_too_long"
    VOICE_ERROR = "voice.error"

    AUDIO_INPUT_OVERRUN = "audio.input_overrun"
    AUDIO_OUTPUT_UNDERRUN = "audio.output_underrun"
    AUDIO_INPUT_QUEUE_DEPTH = "audio.input_queue_depth"
    TTS_FIRST_AUDIO = "speech.tts_first_audio_latency"
    BARGE_IN = "speech.barge_in"

    # --- Telephony (Milestone 4) -------------------------------------------
    # Labels here are closed sets only — a cause, a codec, an outcome. A phone
    # number, caller name, patient id, appointment id or SIP URI as a label
    # would turn the metrics endpoint into a caller log, and cardinality would
    # grow with every call. `call_id` is for structured logs, not metrics.
    TELEPHONY_CALLS_STARTED = "telephony.calls_started"
    TELEPHONY_CALLS_ANSWERED = "telephony.calls_answered"
    TELEPHONY_CALLS_COMPLETED = "telephony.calls_completed"
    TELEPHONY_CALLS_FAILED = "telephony.calls_failed"
    TELEPHONY_CALLS_REJECTED = "telephony.calls_rejected"
    TELEPHONY_CALLS_TRANSFERRED = "telephony.calls_transferred"
    TELEPHONY_TRANSFER_FAILED = "telephony.transfer_failed"
    TELEPHONY_ACTIVE_CALLS = "telephony.active_calls"
    TELEPHONY_CALL_DURATION = "telephony.call_duration"
    TELEPHONY_ANSWER_TO_FIRST_AUDIO = "telephony.answer_to_first_audio"
    #: First audio of a reply entering the transport -> first byte on the wire.
    #: The transport's own share of perceived latency: resampler batching, the
    #: outbound queue and the pacer. Everything past the socket — Asterisk
    #: buffering, the SIP leg, the handset — is not observable from here.
    TELEPHONY_REPLY_WIRE_LATENCY = "telephony.reply_wire_latency"
    TELEPHONY_INBOUND_FRAMES = "telephony.inbound_audio_frames"
    TELEPHONY_OUTBOUND_FRAMES = "telephony.outbound_audio_frames"
    TELEPHONY_AUDIO_OVERRUN = "telephony.audio_overrun"
    TELEPHONY_AUDIO_UNDERRUN = "telephony.audio_underrun"
    TELEPHONY_BARGE_IN_LATENCY = "telephony.barge_in_latency"
    TELEPHONY_MEDIA_ERROR = "telephony.media_error"
    TELEPHONY_DISCONNECTS = "telephony.disconnects"
    TELEPHONY_PBX_RECONNECTS = "telephony.pbx_reconnects"
    TELEPHONY_DTMF = "telephony.dtmf_received"
