import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import websockets

from app.domain.entities import TrackerPosition
from app.domain.errors import SourceConnectionError
from app.infrastructure.navixy_parse import parse_websocket_event

logger = logging.getLogger(__name__)

DEFAULT_IDLE_TIMEOUT_SECONDS = 5.0
_PREVIEW_LIMIT = 80

# Navixy's documented client opens Atmosphere with tracking id 0, which
# negotiates the handshake, and leaves message-length tracking off.
# Length prefixes are still accepted when a server sends them.
_ATMOSPHERE_QUERY = (
    ("X-Atmosphere-tracking-id", "0"),
    ("X-Atmosphere-Framework", "2.3.6-javascript"),
    ("X-Atmosphere-Transport", "websocket"),
    ("Content-Type", "application/json"),
    ("X-atmo-protocol", "true"),
)
_HANDSHAKE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\|(\d+)\|[^|]*\|$"
)
_LENGTH_PREFIX = re.compile(r"^(\d+)\|(.*)$", re.DOTALL)


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


def with_atmosphere_tracking(url: str) -> str:
    """Add Navixy's Atmosphere handshake query if the URL does not set it.

    ``X-Atmosphere-tracking-id=0`` asks for the ``uuid|interval|X|``
    handshake. ``X-Atmosphere-TrackMessageSize`` is not set, so length
    tracking stays off unless the configured URL already asks for it.
    """

    parts = urlsplit(url)
    pairs = parse_qsl(parts.query, keep_blank_values=True)
    present = {key.lower() for key, _value in pairs}
    for key, value in _ATMOSPHERE_QUERY:
        if key.lower() not in present:
            pairs.append((key, value))
    return urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urlencode(pairs), parts.fragment)
    )


@dataclass(frozen=True)
class NavixyFrame:
    """JSON events from one websocket message, plus liveness."""

    events: tuple[dict[str, Any], ...]
    heartbeat: bool
    handshake: bool
    unparseable: tuple[str, ...]

    @property
    def alive(self) -> bool:
        return self.heartbeat or self.handshake or bool(self.events)


def inspect_navixy_frame(raw: str) -> NavixyFrame:
    """Split Atmosphere heartbeats, handshakes, and length prefixes.

    ``X`` and whitespace keep the socket alive and are not events. A
    length prefix is ``<len>|<payload>`` and one websocket message may
    hold several of those back to back.
    """

    if raw.strip() in {"", "X"}:
        return NavixyFrame((), True, False, ())
    events: list[dict[str, Any]] = []
    unparseable: list[str] = []
    heartbeat = False
    handshake = False
    for piece in _length_pieces(raw):
        kind, event = _classify(piece)
        if kind == "heartbeat":
            heartbeat = True
            continue
        if kind == "handshake":
            handshake = True
            continue
        if kind == "event" and event is not None:
            events.append(event)
            continue
        unparseable.append(_preview(piece))
    return NavixyFrame(tuple(events), heartbeat, handshake, tuple(unparseable))


def _length_pieces(raw: str) -> list[str]:
    pieces: list[str] = []
    text = raw
    while text:
        matched = _LENGTH_PREFIX.match(text)
        if matched is None:
            pieces.append(text)
            break
        size = int(matched.group(1))
        rest = matched.group(2)
        if size > len(rest):
            pieces.append(text)
            break
        pieces.append(rest[:size])
        text = rest[size:]
    return pieces


def _classify(piece: str) -> tuple[str, dict[str, Any] | None]:
    stripped = piece.strip()
    if stripped in {"", "X"}:
        return "heartbeat", None
    if _HANDSHAKE.match(stripped):
        return "handshake", None
    try:
        event = json.loads(stripped)
    except json.JSONDecodeError:
        return "bad", None
    if not isinstance(event, dict):
        return "bad", None
    return "event", event


def _preview(text: str) -> str:
    collapsed = " ".join(text.split())
    if len(collapsed) <= _PREVIEW_LIMIT:
        return collapsed
    return collapsed[:_PREVIEW_LIMIT] + "..."


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

        url = with_atmosphere_tracking(self.ws_url)
        try:
            socket = await websockets.connect(url)
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
        self._pending_events: list[dict[str, Any]] = []
        self.last_alive = 0.0

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
        if self._pending_events:
            return self._pending_events.pop(0)
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
        if not isinstance(raw_message, str):
            logger.warning(
                "Ignoring unparseable Navixy websocket frame preview=%s",
                _preview(str(raw_message)),
            )
            return None
        frame = inspect_navixy_frame(raw_message)
        if frame.alive:
            self.last_alive = time.monotonic()
        if frame.handshake:
            logger.debug("Navixy websocket Atmosphere handshake")
        elif frame.heartbeat:
            logger.debug("Navixy websocket heartbeat")
        for preview in frame.unparseable:
            logger.warning(
                "Ignoring unparseable Navixy websocket frame preview=%s",
                preview,
            )
        if not frame.events:
            return None
        self._pending_events.extend(frame.events[1:])
        return frame.events[0]

    async def close(self) -> None:
        pending = self._pending
        self._pending = None
        if pending is not None and not pending.done():
            pending.cancel()
        try:
            await self._socket.close()
        except Exception:
            logger.debug("Navixy websocket close failed", exc_info=True)
