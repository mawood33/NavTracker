import asyncio
import json
import logging
from typing import Any

import websockets

from app.domain.entities import TrackerPosition
from app.domain.errors import SourceConnectionError
from app.infrastructure.navixy_parse import parse_websocket_event

logger = logging.getLogger(__name__)

DEFAULT_IDLE_TIMEOUT_SECONDS = 5.0


def build_subscribe_message(
    api_key: str,
    tracker_ids: list[int],
) -> dict[str, Any]:
    """Subscribe only to tracker_ids. Never uses target type "all"."""

    ids = list(tracker_ids)

    def target() -> dict[str, Any]:
        return {"type": "selected", "tracker_ids": list(ids)}

    return {
        "action": "subscribe",
        "hash": api_key,
        "requests": [
            {"type": "state_batch", "target": target()},
            {"type": "readings_batch", "target": target()},
        ],
    }


def _log_socket_failure(
    operation: str,
    ws_url: str,
    tracker_ids: list[int] | set[int],
    exc: BaseException,
) -> None:
    """Log a Navixy socket failure with context and its traceback.

    ``stream_positions`` used to swallow these and sleep. The supervisor
    reconnects only when ``SourceConnectionError`` is raised, so the
    exception has to be visible here first.
    """

    logger.exception(
        "Navixy websocket %s failed url=%s tracker_ids=%s error=%s",
        operation,
        ws_url,
        sorted(tracker_ids),
        exc,
    )


class NavixyWebSocketClient:
    def __init__(
        self,
        ws_url: str,
        api_key: str,
        idle_timeout: float = DEFAULT_IDLE_TIMEOUT_SECONDS,
    ):
        self.ws_url = ws_url
        self.api_key = api_key
        self.idle_timeout = idle_timeout

    async def connect(self, tracker_ids: list[int]) -> "NavixySubscription":
        if not tracker_ids:
            raise ValueError(
                "tracker_ids is empty; refusing to subscribe to every "
                "Navixy tracker"
            )

        try:
            socket = await websockets.connect(self.ws_url)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _log_socket_failure(
                "connection",
                self.ws_url,
                tracker_ids,
                exc,
            )
            raise SourceConnectionError(
                f"Navixy websocket connection failed: {exc}"
            ) from exc

        subscription = NavixySubscription(
            socket,
            set(tracker_ids),
            idle_timeout=self.idle_timeout,
            ws_url=self.ws_url,
        )
        try:
            await socket.send(
                json.dumps(
                    build_subscribe_message(self.api_key, tracker_ids)
                )
            )
        except asyncio.CancelledError:
            await subscription.close()
            raise
        except Exception as exc:
            await subscription.close()
            _log_socket_failure(
                "subscribe",
                self.ws_url,
                tracker_ids,
                exc,
            )
            raise SourceConnectionError(
                f"Navixy websocket subscribe failed: {exc}"
            ) from exc

        logger.info(
            "Subscribed to Navixy websocket for tracker_ids=%s",
            list(tracker_ids),
        )
        return subscription


class NavixySubscription:
    def __init__(
        self,
        socket: Any,
        tracker_ids: set[int],
        idle_timeout: float,
        ws_url: str = "",
    ):
        self._socket = socket
        self._tracker_ids = tracker_ids
        self._idle_timeout = idle_timeout
        self._ws_url = ws_url
        self._pending: asyncio.Task[Any] | None = None
        self._queued: list[TrackerPosition] = []

    async def receive(self) -> TrackerPosition | None:
        if self._queued:
            return self._queued.pop(0)
        event = await self._next_event()
        if event is None:
            return None
        positions = [
            position
            for position in parse_websocket_event(event)
            if position.tracker_id in self._tracker_ids
        ]
        if not positions:
            return None
        self._queued.extend(positions[1:])
        return positions[0]

    async def _next_event(self) -> dict[str, Any] | None:
        try:
            if self._pending is None or self._pending.done():
                self._pending = asyncio.create_task(self._socket.recv())
            done, _pending = await asyncio.wait(
                {self._pending},
                timeout=self._idle_timeout,
            )
            if not done:
                return None
            task = done.pop()
            self._pending = None
            raw_message = task.result()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _log_socket_failure(
                "receive",
                self._ws_url,
                self._tracker_ids,
                exc,
            )
            raise SourceConnectionError(
                f"Navixy websocket receive failed: {exc}"
            ) from exc

        if isinstance(raw_message, bytes):
            raw_message = raw_message.decode("utf-8", errors="replace")
        try:
            event = json.loads(raw_message)
        except json.JSONDecodeError:
            logger.warning("Ignoring non-JSON Navixy websocket frame")
            return None
        if not isinstance(event, dict):
            return None
        return event

    async def close(self) -> None:
        pending = self._pending
        self._pending = None
        if pending is not None and not pending.done():
            pending.cancel()
        try:
            await self._socket.close()
        except Exception:
            logger.debug("Navixy websocket close failed", exc_info=True)
