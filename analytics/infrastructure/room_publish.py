"""Publish one analytics notice to a ChatSurfer room.

The first attempt returns to the caller. Two further attempts use the
same short backoff as tracker publication. After that the notice is
logged and dropped. A failure stays inside this adapter so a dead room
does not look like a read failure.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable

from navtracker_common.chatsurfer import ChatSurferApiClient
from navtracker_common.errors import PublicationError

logger = logging.getLogger(__name__)

PUBLISH_MAX_ATTEMPTS = 3
PUBLISH_BACKOFF_INITIAL_SECONDS = 0.5
PUBLISH_BACKOFF_CAP_SECONDS = 2.0


class ChatRoomPublisher:
    """Room publisher backed by the shared ChatSurfer client."""

    def __init__(
        self,
        client: ChatSurferApiClient,
        *,
        attempts: int = PUBLISH_MAX_ATTEMPTS,
        initial: float = PUBLISH_BACKOFF_INITIAL_SECONDS,
        cap: float = PUBLISH_BACKOFF_CAP_SECONDS,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        self._client = client
        self._attempts = attempts
        self._initial = initial
        self._cap = cap
        self._sleep = sleep or asyncio.sleep

    async def publish_to(self, room: str, text: str) -> None:
        for attempt in range(1, self._attempts + 1):
            try:
                await self._client.publish_text(text, room_name=room)
                return
            except PublicationError:
                if attempt >= self._attempts:
                    logger.exception(
                        "ChatSurfer publish to %s dropped after %s attempts",
                        room,
                        attempt,
                    )
                    return
                await self._sleep(_backoff(attempt, self._initial, self._cap))


def _backoff(failed_attempts: int, initial: float, cap: float) -> float:
    delay = initial * (1 << max(failed_attempts, 1) - 1)
    if delay < cap:
        return delay
    return cap
