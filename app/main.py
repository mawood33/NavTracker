import asyncio
import logging

from app.application.services import (
    TrackerPollingService,
    TrackerWebSocketService,
)
from app.infrastructure.chatsurfer import ChatSurferApiClient
from app.infrastructure.config import load_settings
from app.infrastructure.crypto import CryptoHandler
from app.infrastructure.navixy_api import NavixyApiClient
from app.infrastructure.navixy_ws import NavixyWebSocketClient


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)


async def main() -> None:
    settings = load_settings()

    crypto_handler = None
    if settings.chatsurfer_encryption_required:
        if settings.mode == "ssh":
            crypto_handler = CryptoHandler(
                mode=settings.mode,
                public_key_path=settings.encryption_cert,
                private_key_path=None,
            )
        elif settings.mode == "fernet":
            crypto_handler = CryptoHandler(
                mode=settings.mode,
                public_key_path=None,
                private_key_path=None,
            )
        else:
            crypto_handler = CryptoHandler(
                public_key_path=settings.encryption_cert,
                private_key_path=None,
            )

    chat_client = ChatSurferApiClient(
        base_url=settings.chatsurfer_base_url,
        api_key=settings.chatsurfer_api_key,
        domain_id=settings.chatsurfer_domain_id,
        room_name=settings.chatsurfer_room_name,
        room_name_encrypted=settings.chatsurfer_room_name_encrypted,
        nickname=settings.chatsurfer_nickname,
        classification=settings.chatsurfer_classification,
        user_high_classification=(
            settings.chatsurfer_user_high_classification
        ),
        rate_limit=settings.chatsurfer_rate_limit,
        max_message_size=settings.chatsurfer_max_message_size,
        encryption_required=settings.chatsurfer_encryption_required,
        crypto_handler=crypto_handler,
        client_cert=settings.chatsurfer_client_cert,
        client_key=settings.chatsurfer_client_key,
    )

    navixy_api = NavixyApiClient(
        base_url=settings.navixy_base_url,
        api_key=settings.navixy_api_key,
    )

    try:
        if settings.source_mode.lower() == "websocket":
            source = NavixyWebSocketClient(
                ws_url=settings.navixy_ws_url,
                api_key=settings.navixy_api_key,
            )

            service = TrackerWebSocketService(
                source=source,
                publisher=chat_client,
                tracker_ids=settings.tracker_ids,
            )
        else:
            service = TrackerPollingService(
                source=navixy_api,
                publisher=chat_client,
                tracker_ids=settings.tracker_ids,
                interval_seconds=settings.poll_interval_seconds,
            )
        logging.info(f'Tracker API source mode: {settings.source_mode}')
        await service.run()
    except Exception as e:
        logging.exception(f'Error starting app: {e}')
    finally:
        await navixy_api.close()
        await chat_client.close()


if __name__ == "__main__":
    asyncio.run(main())
