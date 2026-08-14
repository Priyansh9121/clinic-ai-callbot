"""Starting telephony inside the application, and giving each call its own
conversation stack.

Everything below the transport already existed; this is the wiring that was
missing. ``build_telephony_service`` had no caller, so the ARI event loop and
the media server were never started and ``/health`` always reported
``telephony: service not started``.

The interesting part is the voice factory. ``CallSession`` asks for a
``VoiceSession`` when a call connects, and that session needs a
``ConversationEngine``, which needs a database session. Those cannot be shared:
concurrent calls on one ``AsyncSession`` serialise, and the measured symptom is
the second caller getting no audio at all. So each call opens its own session
and closes it when the call ends — the same one-scope-per-call shape the local
voice CLI has used since Milestone 3.

Nothing here contains clinic logic. It builds the same objects the text and
microphone paths build, from the same factories.
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING, Any

from app.clinic.factory import build_clinic_knowledge
from app.conversation.engine import ConversationEngine
from app.conversation.nlu.baseline import MessageAnalyser
from app.conversation.store import SessionStore
from app.core.config import Settings
from app.core.errors import ProviderUnavailableError
from app.core.logging import get_logger
from app.database.session import get_sessionmaker
from app.safety.engine import SafetyEngine
from app.speech.providers import SpeechToTextProvider, TextToSpeechProvider
from app.telephony.factory import build_telephony_service
from app.voice.factory import session_config
from app.voice.session import VoiceSession

if TYPE_CHECKING:
    from app.telephony.service import TelephonyService

logger = get_logger(__name__)


class TelephonyVoiceFactory:
    """Builds one ``VoiceSession`` per call, with its own database session.

    Speech providers (Whisper, Piper, Silero) are built once and shared: they
    are stateless per call and internally concurrency-bounded, and loading
    Whisper's weights per call would add seconds to every answer.
    """

    def __init__(
        self, *, settings: Settings, analyser: MessageAnalyser, store: SessionStore
    ) -> None:
        self._settings = settings
        self._analyser = analyser
        self._store = store
        # Telephony is the one path where a placeholder answer reaches a real
        # patient, so the source is chosen deliberately rather than defaulted.
        self._knowledge = build_clinic_knowledge(settings)
        self._safety = SafetyEngine()

        # Built once, shared across calls.
        from app.speech.factory import build_stt, build_tts, build_vad

        stt = build_stt(settings)
        tts = build_tts(settings)
        # Checked here rather than per call: telephony_configured already
        # requires all three, so a None at this point is a configuration bug
        # and must surface at start-up, not as an AttributeError once a caller
        # is already on the line.
        if stt is None or tts is None:
            missing = [n for n, p in (("stt", stt), ("tts", tts)) if p is None]
            raise ProviderUnavailableError(
                detail=f"voice providers not configured: {', '.join(missing)}",
                public_message="The voice pipeline is not configured.",
            )
        self._stt: SpeechToTextProvider = stt
        self._tts: TextToSpeechProvider = tts
        self._vad_settings = settings
        self._build_vad = build_vad

        #: Per-call database sessions, keyed by the VoiceSession that owns one.
        self._open: dict[int, Any] = {}

    def __call__(self, audio_in: Any, audio_out: Any, on_event: Any) -> VoiceSession:
        vad = self._build_vad(self._vad_settings)
        if vad is None:  # pragma: no cover - guarded at construction
            raise ProviderUnavailableError(
                detail="voice providers not configured: vad",
                public_message="The voice pipeline is not configured.",
            )
        sessionmaker = get_sessionmaker()
        db = sessionmaker()
        engine = ConversationEngine(
            db=db,
            settings=self._settings,
            store=self._store,
            safety=self._safety,
            analyser=self._analyser,
            knowledge=self._knowledge,
        )
        voice = VoiceSession(
            engine=engine,
            audio_input=audio_in,
            audio_output=audio_out,
            # VAD holds per-call state, so it is built per call. STT and TTS
            # do not, and are shared.
            vad=vad,
            stt=self._stt,
            tts=self._tts,
            config=session_config(self._settings, greet=True),
            on_event=on_event,
        )
        self._open[id(voice)] = db
        return voice

    async def release(self, voice: object) -> None:
        """Close the database session a finished call was using."""
        db = self._open.pop(id(voice), None)
        if db is None:
            return
        with contextlib.suppress(Exception):
            await db.close()


async def start_telephony(app_state: Any, settings: Settings) -> TelephonyService | None:
    """Start the telephony stack, or return None if it is not configured.

    Failure to start telephony must never stop the backend: the text API and
    the local voice CLI keep working, ``/health`` reports telephony down, and
    the dialplan's health-check failover sends callers to reception. That is
    the arrangement documented since Milestone 1, and it only holds if this
    function declines rather than raises.
    """
    if not settings.telephony_configured:
        return None

    factory = TelephonyVoiceFactory(
        settings=settings,
        analyser=app_state.analyser,
        store=app_state.session_store,
    )
    try:
        service = build_telephony_service(settings=settings, voice_factory=factory)
        service.voice_factory_owner = factory  # type: ignore[attr-defined]
        await service.start()
    except Exception as exc:
        logger.error("telephony.start_failed", error=type(exc).__name__, detail=str(exc))
        return None

    logger.info(
        "telephony.started",
        stasis_app=settings.asterisk_app_name,
        media_bind=f"{settings.telephony_media_host}:{settings.telephony_media_port}",
        media_advertised=f"{settings.media_advertise_host}:{settings.telephony_media_port}",
        wire_format=settings.telephony_wire_format,
        max_calls=settings.telephony_max_active_calls,
    )
    return service


async def stop_telephony(service: TelephonyService | None) -> None:
    if service is None:
        return
    with contextlib.suppress(Exception):
        await service.stop()
