"""Build the telephony stack from configuration.

Keeps construction out of the service and the CLI, mirroring
``app/voice/factory.py``. Nothing here holds state.
"""

from __future__ import annotations

from app.core.config import Settings
from app.core.errors import ProviderUnavailableError
from app.telephony.ari import AriClient
from app.telephony.destinations import DestinationRegistry, TransferDestination
from app.telephony.manager import CallManager, CapacityPolicy
from app.telephony.server import AudioSocketServer
from app.telephony.service import TelephonyService
from app.telephony.transport import TelephonyFormat


def wire_format(settings: Settings) -> TelephonyFormat:
    return TelephonyFormat(settings.telephony_wire_format)


def destination_registry(settings: Settings) -> DestinationRegistry:
    """Approved destinations only.

    Raises at construction if configuration supplies something that is not a
    bare dialplan extension, so a bad ``.env`` fails at start-up rather than
    mid-call — and can never become an outbound dial string.
    """
    return DestinationRegistry(
        {
            TransferDestination.RECEPTION: settings.reception_extension,
            TransferDestination.VOICEMAIL: settings.voicemail_extension,
        },
        context=settings.telephony_transfer_context,
    )


def call_manager(settings: Settings) -> CallManager:
    return CallManager(
        max_active_calls=settings.telephony_max_active_calls,
        capacity_policy=CapacityPolicy(settings.telephony_capacity_policy),
    )


def ari_client(settings: Settings) -> AriClient:
    return AriClient(
        base_url=settings.asterisk_ari_url,
        username=settings.asterisk_ari_user,
        password=settings.asterisk_ari_password.get_secret_value(),
        app_name=settings.asterisk_app_name,
    )


def build_telephony_service(
    *,
    settings: Settings,
    voice_factory: object,
) -> TelephonyService:
    """Assemble the service, raising if telephony is not fully configured."""
    if not settings.telephony_configured:
        missing = []
        if not settings.telephony_enabled:
            missing.append("TELEPHONY_ENABLED")
        if not settings.voice_configured:
            missing.append("voice providers (STT/TTS/VAD)")
        if not settings.asterisk_ari_user:
            missing.append("ASTERISK_ARI_USER")
        if not settings.asterisk_ari_password.get_secret_value():
            missing.append("ASTERISK_ARI_PASSWORD")
        raise ProviderUnavailableError(
            detail=f"telephony not configured: {', '.join(missing)}",
            public_message="Telephony is not configured.",
        )

    if not settings.media_advertise_is_routable:
        # A wildcard bind with no advertised address would have us hand
        # Asterisk "0.0.0.0" as a media destination. The call connects, the
        # media channel never does, and the caller hears silence — so fail
        # here, at start-up, where the message can say what to set.
        raise ProviderUnavailableError(
            detail=(
                "TELEPHONY_MEDIA_HOST is a wildcard bind, so "
                "TELEPHONY_MEDIA_ADVERTISE_HOST must be set to the address "
                "Asterisk should dial for media"
            ),
            public_message="Telephony media address is not configured.",
        )

    manager = call_manager(settings)
    service = TelephonyService(
        ari=ari_client(settings),
        media_host=settings.telephony_media_host,
        media_advertise_host=settings.media_advertise_host,
        media_port=settings.telephony_media_port,
        destinations=destination_registry(settings),
        voice_factory=voice_factory,
        manager=manager,
        hangup_drain_ms=settings.telephony_hangup_drain_ms,
        max_call_seconds=settings.telephony_max_call_seconds,
        wire_format=wire_format(settings),
        resample_quality=settings.telephony_resample_quality,
    )
    return service


def media_server(settings: Settings, handler: object) -> AudioSocketServer:
    """Standalone media server, for the diagnostic script and tests."""
    return AudioSocketServer(
        handler,  # type: ignore[arg-type]
        host=settings.telephony_media_host,
        port=settings.telephony_media_port,
        wire_format=wire_format(settings),
        resample_quality=settings.telephony_resample_quality,
        max_connections=settings.telephony_max_active_calls,
    )
