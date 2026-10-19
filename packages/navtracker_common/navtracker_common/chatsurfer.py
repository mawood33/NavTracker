import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import httpx

from navtracker_common.crypto import CryptoHandler
from navtracker_common.errors import PublicationError
from navtracker_common.limiter import RateLimiter
from navtracker_common.publication import OutboundMessage

logger = logging.getLogger(__name__)

# Assumed read route. ChatSurfer's published contract in this repo is the
# POST /api/chatserver/message writer. The reader uses the same host, API
# key, and mTLS material, and looks for a JSON list of room messages.
MESSAGES_PATH = "/api/chatserver/messages"


class ChatReadError(Exception):
    """A ChatSurfer room could not be read. Not a publish failure."""


@dataclass(frozen=True)
class ChatRoomMessage:
    message_id: str
    room_name: str
    body: str
    nickname: str
    timestamp: datetime | None


class ChatSurferApiClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        domain_id: str,
        room_name: str,
        room_name_encrypted: str,
        nickname: str,
        classification: str,
        user_high_classification: str,
        rate_limit: float,
        max_message_size: int,
        encryption_required: bool,
        crypto_handler: CryptoHandler | None = None,
        timeout_seconds: float = 15,
        client_cert: str | None = None,
        client_key: str | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.domain_id = domain_id
        self.room_name = room_name
        self.room_name_encrypted = room_name_encrypted
        self.nickname = nickname
        self.classification = classification
        self.user_high_classification = user_high_classification
        self.max_message_size = max_message_size
        self.encryption_required = encryption_required
        self.crypto_handler = crypto_handler
        self.rate_limiter = RateLimiter(rate_limit)

        self.client = httpx.AsyncClient(
            timeout=timeout_seconds,
            headers={
                "Api-Key": f"{api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            cert=self._cert_tuple(client_cert, client_key),
        )

    @staticmethod
    def _cert_tuple(
        client_cert: str | None, client_key: str | None
    ) -> tuple[str, str] | None:
        if not client_cert or not client_key:
            return None
        return (client_cert, client_key)

    async def close(self) -> None:
        await self.client.aclose()

    async def publish(self, message: OutboundMessage) -> dict[str, Any]:
        try:
            return await self._publish_message(message)
        except asyncio.CancelledError:
            raise
        except PublicationError:
            raise
        except Exception as exc:
            raise PublicationError(
                "ChatSurfer publish failed for "
                f"tracker_id={message.tracker_id}: {exc}"
            ) from exc

    async def publish_text(self, message: str) -> dict[str, Any]:
        """Publish an already-serialized document to the configured room."""

        try:
            return await self._post_plaintext(message)
        except asyncio.CancelledError:
            raise
        except PublicationError:
            raise
        except Exception as exc:
            raise PublicationError(f"ChatSurfer publish failed: {exc}") from exc

    async def _publish_message(
        self,
        outbound: OutboundMessage,
    ) -> dict[str, Any]:
        message = json.dumps(
            outbound.to_payload(),
            separators=(",", ":"),
        )
        return await self._post_plaintext(message)

    async def _post_plaintext(self, message: str) -> dict[str, Any]:
        room_name = self.room_name
        if self.encryption_required:
            if self.crypto_handler is None:
                raise PublicationError(
                    "Encryption is required but CryptoHandler is unavailable"
                )

            message = self.crypto_handler.encrypt(message)
            room_name = self.room_name_encrypted

        message = self._fit_message_limit(message)
        logger.debug(
            "Publish to ChatSurfer: room-%s, nickname-%s, encryption-%s",
            self.room_name,
            self.nickname,
            self.encryption_required,
        )

        await self.rate_limiter.acquire()

        response = await self.client.post(
            f"{self.base_url}/api/chatserver/message",
            json={
                "classification": self.classification,
                "domainId": self.domain_id,
                "message": message,
                "nickname": self.nickname,
                "roomName": room_name,
                "userHighClassification": self.user_high_classification
            },
        )

        return self._handle_response(response)

    async def read_room(
        self,
        room_name: str,
        *,
        since: str | None = None,
        limit: int = 100,
    ) -> list[ChatRoomMessage]:
        """Read one room. Decrypts message bodies when encryption is on.

        ``since`` is passed through as the ``since`` query parameter. The
        service returns whatever the room endpoint uses as its cursor.
        """

        params: dict[str, str] = {
            "roomName": room_name,
            "domainId": self.domain_id,
            "limit": str(limit),
        }
        if since:
            params["since"] = since
        await self.rate_limiter.acquire()
        try:
            response = await self.client.get(
                f"{self.base_url}{MESSAGES_PATH}",
                params=params,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise ChatReadError("ChatSurfer room read failed") from exc
        if not response.is_success:
            raise ChatReadError(
                f"ChatSurfer room read HTTP {response.status_code}"
            )
        try:
            body = response.json()
        except ValueError as exc:
            raise ChatReadError("ChatSurfer room read was not JSON") from exc
        return [
            self._decode_room_message(item, room_name)
            for item in _message_entries(body)
        ]

    def _decode_room_message(
        self,
        item: object,
        fallback_room: str,
    ) -> ChatRoomMessage:
        if not isinstance(item, dict):
            raise ChatReadError("ChatSurfer room message was not an object")
        raw_body = item.get("message", "")
        if not isinstance(raw_body, str):
            raw_body = json.dumps(raw_body, separators=(",", ":"))
        body = self._maybe_decrypt(raw_body)
        message_id = str(item.get("id", ""))
        room = item.get("roomName", fallback_room)
        nickname = item.get("nickname", "")
        stamp = _parse_timestamp(item.get("timestamp"))
        return ChatRoomMessage(
            message_id=message_id,
            room_name=room if isinstance(room, str) else fallback_room,
            body=body,
            nickname=nickname if isinstance(nickname, str) else "",
            timestamp=stamp,
        )

    def _maybe_decrypt(self, body: str) -> str:
        if not self.encryption_required or self.crypto_handler is None:
            return body
        try:
            payload = json.loads(body)
        except ValueError:
            return body
        if not isinstance(payload, dict) or "algorithm" not in payload:
            return body
        return self.crypto_handler.decrypt(body)

    def _fit_message_limit(self, message: str) -> str:
        if len(message) <= self.max_message_size:
            return message

        raise PublicationError(
            "Message exceeds ChatSurfer message-size limit: "
            f"{len(message)} > {self.max_message_size}"
        )

    @staticmethod
    def _handle_response(response: httpx.Response) -> dict[str, Any]:
        try:
            body = response.json()
        except ValueError:
            body = {"text": response.text}

        if response.is_success:
            return {
                "data": body,
                "rate_limit": {
                    "user_limit": response.headers.get(
                        "X-RateLimit-UserLimit"
                    ),
                    "user_remaining": response.headers.get(
                        "X-RateLimit-UserRemaining"
                    ),
                    "user_reset": response.headers.get(
                        "X-RateLimit-UserReset"
                    ),
                },
            }

        raise PublicationError(
            f"ChatSurfer HTTP {response.status_code}: {body}"
        )


def _message_entries(body: object) -> list[object]:
    if isinstance(body, list):
        return body
    if isinstance(body, dict):
        messages = body.get("messages", body.get("data"))
        if isinstance(messages, list):
            return messages
    raise ChatReadError("ChatSurfer room read did not include messages")


def _parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None
