import json
from typing import Any

import httpx

from app.domain.entities import TrackerPosition
from .crypto import CryptoHandler
from .limiter import RateLimiter


class ChatSurferApiClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        domain_id: str,
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

    async def publish(self, position: TrackerPosition) -> dict[str, Any]:
        message = json.dumps(
            position.as_dict(),
            separators=(",", ":"),
        )

        room_name = "freedom_flag_encrypted"

        if self.encryption_required:
            if self.crypto_handler is None:
                raise RuntimeError(
                    "Encryption is required but CryptoHandler is unavailable"
                )

            message = self.crypto_handler.encrypt(message)
            room_name = "freedom_flag_encrypted"

        message = self._fit_message_limit(message)

        await self.rate_limiter.acquire()

        response = await self.client.post(
            f"{self.base_url}/api/chatserver/message",
            json={
                "classification": self.classification,
                "domainId": self.domain_id,
                "message": message,
                "nickName": self.nickname,
                "roomName": room_name,
                "userHighClassification": self.user_high_classification
            },
        )

        return self._handle_response(response)

    def _fit_message_limit(self, message: str) -> str:
        if len(message) <= self.max_message_size:
            return message

        raise ValueError(
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

        raise RuntimeError(
            f"ChatSurfer HTTP {response.status_code}: {body}"
        )
