import asyncio
import json
import logging
from datetime import datetime
from typing import Any

import websockets

from app.domain.entities import TrackerPosition
from app.domain.errors import SourceConnectionError

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


def parse_event(event: dict[str, Any]) -> TrackerPosition | None:
    if event.get("type") not in (None, "location"):
        return None

    tracker_id = event.get("tracker_id")
    latitude = event.get("lat")
    longitude = event.get("lng")

    if tracker_id is None or latitude is None or longitude is None:
        return None

    timestamp_value = event.get("timestamp")
    timestamp = (
        datetime.fromisoformat(timestamp_value.replace("Z", "+00:00"))
        if timestamp_value
        else None
    )

    return TrackerPosition(
        tracker_id=int(tracker_id),
        latitude=float(latitude),
        longitude=float(longitude),
        timestamp=timestamp,
        speed=event.get("speed"),
        heading=event.get("heading"),
        altitude=event.get("altitude"),
        raw=event,
    )


def position_in_scope(
    event: dict[str, Any],
    tracker_ids: set[int],
) -> TrackerPosition | None:
    position = parse_event(event)
    if position is None or position.tracker_id not in tracker_ids:
        return None
    return position


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
            raise SourceConnectionError(
                f"Navixy websocket connection failed: {exc}"
            ) from exc

        subscription = NavixySubscription(
            socket,
            set(tracker_ids),
            idle_timeout=self.idle_timeout,
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
    ):
        self._socket = socket
        self._tracker_ids = tracker_ids
        self._idle_timeout = idle_timeout
        self._pending: asyncio.Task[Any] | None = None

    async def receive(self) -> TrackerPosition | None:
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
        return position_in_scope(event, self._tracker_ids)

    async def close(self) -> None:
        pending = self._pending
        self._pending = None
        if pending is not None and not pending.done():
            pending.cancel()
        try:
            await self._socket.close()
        except Exception:
            logger.debug("Navixy websocket close failed", exc_info=True)
