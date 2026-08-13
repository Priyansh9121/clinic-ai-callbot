"""Assemble a live ``VoiceSession`` from configuration.

Keeps device and provider construction out of the CLI, so the CLI stays a thin
front end and Milestone 4 can build a telephony session the same way — by
passing a different ``AudioInput``/``AudioOutput`` pair.
"""

from __future__ import annotations

from app.conversation.engine import ConversationEngine
from app.core.config import Settings
from app.core.errors import ProviderUnavailableError
from app.speech.factory import build_stt, build_tts, build_vad
from app.voice.endpointing import EndpointConfig
from app.voice.session import VoiceSession, VoiceSessionConfig
from app.voice.transport import AudioInput, AudioOutput


def endpoint_config(settings: Settings) -> EndpointConfig:
    return EndpointConfig(
        end_silence_ms=settings.voice_end_silence_ms,
        initial_silence_ms=settings.voice_initial_silence_ms,
        min_speech_ms=settings.voice_min_speech_ms,
        max_utterance_ms=settings.voice_max_utterance_ms,
    )


def session_config(settings: Settings, *, greet: bool = True) -> VoiceSessionConfig:
    return VoiceSessionConfig(
        endpoint=endpoint_config(settings),
        acknowledgement_after_ms=settings.voice_acknowledgement_after_ms,
        acknowledgement_enabled=settings.voice_acknowledgement_enabled,
        barge_in_frames=settings.voice_barge_in_frames,
        greet_on_start=greet,
    )


def build_local_devices(settings: Settings) -> tuple[AudioInput, AudioOutput]:
    """Microphone and speaker transports for the local demo."""
    from app.voice.devices import MicrophoneAudioInput, SpeakerAudioOutput

    return (
        MicrophoneAudioInput(device=settings.device_or_default(settings.voice_input_device)),
        SpeakerAudioOutput(device=settings.device_or_default(settings.voice_output_device)),
    )


def build_voice_session(
    *,
    settings: Settings,
    engine: ConversationEngine,
    audio_input: AudioInput | None = None,
    audio_output: AudioOutput | None = None,
    greet: bool = True,
    on_event=None,
) -> VoiceSession:
    """Build a session, raising if the voice stack is not fully configured.

    All three providers are required together: hearing without speaking, or
    either without VAD, is not a usable phone agent, so a partial configuration
    fails loudly here rather than halfway through a call.
    """
    stt = build_stt(settings)
    tts = build_tts(settings)
    vad = build_vad(settings)
    missing = [
        name for name, provider in (("stt", stt), ("tts", tts), ("vad", vad)) if provider is None
    ]
    if missing:
        raise ProviderUnavailableError(
            detail=f"voice providers not configured: {', '.join(missing)}",
            public_message="The voice pipeline is not configured.",
        )

    if audio_input is None or audio_output is None:
        audio_input, audio_output = build_local_devices(settings)

    return VoiceSession(
        engine=engine,
        audio_input=audio_input,
        audio_output=audio_output,
        vad=vad,
        stt=stt,
        tts=tts,
        config=session_config(settings, greet=greet),
        on_event=on_event,
    )
