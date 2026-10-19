from dataclasses import dataclass

from app.application.supervisor import IngestionSupervisor
from app.infrastructure.chatsurfer import ChatSurferApiClient
from app.infrastructure.config import Settings
from app.infrastructure.crypto import CryptoHandler, build_crypto_handler
from app.infrastructure.navixy_api import NavixyApiClient
from app.infrastructure.navixy_ws import NavixyWebSocketClient


@dataclass
class Application:
    """Objects created at startup. Nothing here has opened a connection."""

    crypto_handler: CryptoHandler | None
    publisher: ChatSurferApiClient
    reader: NavixyApiClient
    stream: NavixyWebSocketClient
    supervisor: IngestionSupervisor

    async def aclose(self) -> None:
        await self.reader.close()
        await self.publisher.close()


def build_application(settings: Settings) -> Application:
    """Construct the supervisor, ports, crypto handler, and publisher.

    Clients are created only. This does not call ``run``, ``connect``,
    ``get_position``, or ``publish``, so Navixy and ChatSurfer are not contacted.
    """

    crypto_handler = build_crypto_handler(
        encryption_required=settings.chatsurfer_encryption_required,
        mode=settings.mode,
        public_key_path=settings.encryption_cert,
        fernet_key=settings.fernet_key,
    )
    publisher = ChatSurferApiClient(
        base_url=settings.chatsurfer_base_url,
        api_key=settings.chatsurfer_api_key,
        domain_id=settings.chatsurfer_domain_id,
        room_name=settings.chatsurfer_room_name,
        room_name_encrypted=settings.chatsurfer_room_name_encrypted,
        nickname=settings.chatsurfer_nickname,
        classification=settings.chatsurfer_classification,
        user_high_classification=settings.chatsurfer_user_high_classification,
        rate_limit=settings.chatsurfer_rate_limit,
        max_message_size=settings.chatsurfer_max_message_size,
        encryption_required=settings.chatsurfer_encryption_required,
        crypto_handler=crypto_handler,
        client_cert=settings.chatsurfer_client_cert,
        client_key=settings.chatsurfer_client_key,
    )
    reader = NavixyApiClient(
        base_url=settings.navixy_base_url,
        api_key=settings.navixy_api_key,
    )
    stream = NavixyWebSocketClient(
        ws_url=settings.navixy_ws_url,
        api_key=settings.navixy_api_key,
    )
    supervisor = IngestionSupervisor(
        reader=reader,
        stream=stream,
        publisher=publisher,
        tracker_ids=settings.tracker_ids,
        mode=settings.navixy_mode,
        poll_interval_seconds=settings.poll_interval_seconds,
        policy=settings.failover,
    )
    return Application(
        crypto_handler=crypto_handler,
        publisher=publisher,
        reader=reader,
        stream=stream,
        supervisor=supervisor,
    )
