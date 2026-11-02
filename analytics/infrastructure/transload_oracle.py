"""Optional push of analytics documents to a TransloadOracle ingest URL.

NavTracker does not require this process. Construction does not open a
socket. ``publish`` logs and swallows transport failures so an absent or
unreachable peer cannot stop analysis.
"""

import json
import logging
from collections.abc import Awaitable, Callable, Mapping

import httpx

logger = logging.getLogger(__name__)

Post = Callable[[str, bytes], Awaitable[None]]


class TransloadOraclePublisher:
    """POST the same documents the WebSocket sends. Failures stay local."""

    def __init__(self, url: str, *, post: Post | None = None):
        self.url = url
        self.last_result = ""
        self._post = post
        self._client: httpx.AsyncClient | None = None

    async def publish(self, document: Mapping[str, object]) -> None:
        body = json.dumps(document, separators=(",", ":")).encode("utf-8")
        try:
            if self._post is not None:
                await self._post(self.url, body)
            else:
                if self._client is None:
                    self._client = httpx.AsyncClient(timeout=5)
                response = await self._client.post(
                    self.url,
                    content=body,
                    headers={"Content-Type": "application/json"},
                )
                response.raise_for_status()
        except Exception as exc:
            self.last_result = "unreachable"
            logger.warning(
                "TransloadOracle unreachable (%s); NavTracker is continuing without it",
                exc.__class__.__name__,
            )
            return
        self.last_result = "ok"

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
