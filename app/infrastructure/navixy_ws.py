import asyncio
import json
import logging
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import websockets

from app.domain.entities import TrackerPosition
from app.domain.errors import SourceConnectionError
from app.infrastructure.navixy_parse import parse_websocket_event

logger = logging.getLogger(__name__)

DEFAULT_IDLE_TIMEOUT_SECONDS = 5.0
SUBSCRIPTION_REPLY_TIMEOUT_SECONDS = 30.0
POSITION_STALL_SECONDS = 120.0
_PREVIEW_LIMIT = 80
_STATUS_NAMES = ("normal", "blocked", "unknown", "disallowed")

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
            {
                "type": "state_batch",
                "target": target(),
                "rate_limit": "5s",
                "format": "full",
            },
            {
                "type": "readings_batch",
                "target": target(),
                "rate_limit": "5s",
            },
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


def _as_text(raw_message: object) -> str | None:
    if isinstance(raw_message, bytes):
        return raw_message.decode("utf-8", errors="replace")
    if isinstance(raw_message, str):
        return raw_message
    return None


def _event_name(event: dict[str, Any]) -> str:
    if event.get("type") == "event":
        name = event.get("event")
        if isinstance(name, str) and name:
            return name
    event_type = event.get("type")
    if isinstance(event_type, str) and event_type:
        return event_type
    return "unknown"


def _source_ids(event: dict[str, Any]) -> list[int]:
    data = event.get("data")
    items = data if isinstance(data, list) else [data]
    found: list[int] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        source_id = item.get("source_id")
        if isinstance(source_id, bool) or not isinstance(source_id, int):
            continue
        found.append(source_id)
        if len(found) == 20:
            break
    return found


def _status_counts(
    block: object,
    tracker_ids: set[int],
    source_to_tracker: Mapping[int, int] | None,
) -> dict[str, int]:
    """Count subscription statuses for the selected trackers.

    Navixy documents ``value`` as a map of tracker id to status. JSON
    object keys arrive as strings. A key that is a device source id is
    mapped back to the tracker id. ``normal`` is deliverable. The count
    stays at the raw number of ``normal`` entries when none of the keys
    match the subscription, so a reply that says every tracker is
    normal is not treated as an empty set.
    """

    counts = {name: 0 for name in _STATUS_NAMES}
    value = _status_value(block)
    if value is None:
        return counts
    matched: dict[str, set[int]] = {name: set() for name in _STATUS_NAMES}
    unmatched = {name: 0 for name in _STATUS_NAMES}
    saw_selected = False
    for raw_id, status in value.items():
        if not isinstance(status, str) or status not in matched:
            continue
        tracker_id = _reply_tracker_id(
            raw_id,
            tracker_ids,
            source_to_tracker,
        )
        if tracker_id is None:
            unmatched[status] += 1
            continue
        saw_selected = True
        matched[status].add(tracker_id)
    if saw_selected:
        return {name: len(ids) for name, ids in matched.items()}
    return unmatched


def _status_value(block: object) -> dict[object, object] | None:
    if not isinstance(block, dict):
        return None
    value = block.get("value")
    if isinstance(value, dict):
        return value
    if any(key in block for key in ("success", "type", "action")):
        return None
    if any(isinstance(item, str) and item in _STATUS_NAMES for item in block.values()):
        return block
    return None


def _reply_tracker_id(
    raw_id: object,
    tracker_ids: set[int],
    source_to_tracker: Mapping[int, int] | None,
) -> int | None:
    parsed = _reply_id(raw_id)
    if parsed is None:
        return None
    if parsed in tracker_ids:
        return parsed
    if source_to_tracker is None:
        return None
    mapped = source_to_tracker.get(parsed)
    if mapped in tracker_ids:
        return mapped
    return None


def _reply_id(raw_id: object) -> int | None:
    if isinstance(raw_id, bool):
        return None
    if isinstance(raw_id, int):
        return raw_id
    if isinstance(raw_id, str) and raw_id.strip():
        try:
            return int(raw_id.strip())
        except ValueError:
            return None
    return None


def _error_status(event: dict[str, Any]) -> tuple[object, str]:
    status = event.get("status")
    if not isinstance(status, dict):
        return None, ""
    description = status.get("description")
    if not isinstance(description, str):
        description = ""
    description = " ".join(description.split())
    if len(description) > 120:
        description = description[:120] + "..."
    return status.get("code"), description


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

    async def connect(
        self,
        tracker_ids: list[int],
        source_to_tracker: Mapping[int, int] | None = None,
    ) -> "NavixySubscription":
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
            source_to_tracker=source_to_tracker,
        )
        try:
            await socket.send(
                json.dumps(
                    build_subscribe_message(self.api_key, tracker_ids)
                )
            )
            await subscription.read_subscription_reply()
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

        counts = subscription.subscription_counts
        logger.info(
            "Subscribed to Navixy websocket for tracker_ids=%s "
            "normal=%s blocked=%s unknown=%s disallowed=%s",
            list(tracker_ids),
            counts["normal"],
            counts["blocked"],
            counts["unknown"],
            counts["disallowed"],
        )
        return subscription


class NavixySubscription:
    def __init__(
        self,
        socket: Any,
        tracker_ids: set[int],
        idle_timeout: float,
        ws_url: str = "",
        source_to_tracker: Mapping[int, int] | None = None,
        reply_timeout: float = SUBSCRIPTION_REPLY_TIMEOUT_SECONDS,
        position_stall_seconds: float = POSITION_STALL_SECONDS,
    ):
        self._socket = socket
        self._tracker_ids = tracker_ids
        self._idle_timeout = idle_timeout
        self._ws_url = ws_url
        self._source_to_tracker = (
            None
            if source_to_tracker is None
            else dict(source_to_tracker)
        )
        self._reply_timeout = reply_timeout
        self._stall_seconds = position_stall_seconds
        self._pending: asyncio.Task[Any] | None = None
        self._queued: list[TrackerPosition] = []
        self._pending_events: list[dict[str, Any]] = []
        self._event_counts: dict[str, int] = {}
        self._opened_at = time.monotonic()
        self._last_position_at: float | None = None
        self._quiet_warned = False
        self.last_alive = 0.0
        self.subscription_counts = {name: 0 for name in _STATUS_NAMES}

    async def read_subscription_reply(self) -> None:
        """Read until Navixy accepts the subscribe action.

        Heartbeats and the Atmosphere handshake may arrive first. A
        state event that arrives before the reply is kept for
        ``receive``. The reply itself is logged by the caller.
        """

        deadline = time.monotonic() + self._reply_timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise SourceConnectionError(
                    "Navixy subscription reply timed out"
                )
            try:
                raw_message = await asyncio.wait_for(
                    self._socket.recv(),
                    remaining,
                )
            except TimeoutError as exc:
                raise SourceConnectionError(
                    "Navixy subscription reply timed out"
                ) from exc
            reply = self._take_reply(raw_message)
            if reply is None:
                continue
            self._accept_reply(reply)
            self._opened_at = time.monotonic()
            self._last_position_at = None
            self._quiet_warned = False
            return

    async def receive(self) -> TrackerPosition | None:
        if self._queued:
            self._note_position()
            return self._queued.pop(0)
        if not self._pending_events:
            self._raise_if_stalled()
        event = await self._next_event()
        if event is None:
            return None
        parsed = parse_websocket_event(
            event,
            source_to_tracker=self._source_to_tracker,
        )
        positions = [
            position
            for position in parsed
            if position.tracker_id in self._tracker_ids
        ]
        dropped = len(parsed) - len(positions)
        kind = _event_name(event)
        if kind in {"state", "state_batch"} and not positions:
            logger.warning(
                "Dropping Navixy %s frame with no in-scope position "
                "source_ids=%s",
                kind,
                _source_ids(event),
            )
        elif dropped:
            logger.warning(
                "Dropping Navixy websocket positions outside "
                "subscription count=%s",
                dropped,
            )
        if not positions:
            return None
        self._queued.extend(positions[1:])
        self._note_position()
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
        for event in frame.events:
            self._note_event(_event_name(event))
        self._pending_events.extend(frame.events[1:])
        return frame.events[0]

    def _take_reply(self, raw_message: object) -> dict[str, Any] | None:
        text = _as_text(raw_message)
        if text is None:
            logger.warning(
                "Ignoring unparseable Navixy websocket frame preview=%s",
                _preview(str(raw_message)),
            )
            return None
        frame = inspect_navixy_frame(text)
        if frame.alive:
            self.last_alive = time.monotonic()
        reply: dict[str, Any] | None = None
        for event in frame.events:
            if event.get("type") in {"response", "error"}:
                reply = event
                continue
            self._pending_events.append(event)
            self._note_event(_event_name(event))
        for preview in frame.unparseable:
            logger.warning(
                "Ignoring unparseable Navixy websocket frame preview=%s",
                preview,
            )
        if reply is not None:
            return reply
        if frame.handshake:
            logger.debug("Navixy websocket Atmosphere handshake")
        elif frame.heartbeat:
            logger.debug("Navixy websocket heartbeat")
        return None

    def _accept_reply(self, event: dict[str, Any]) -> None:
        if event.get("type") == "error":
            code, description = _error_status(event)
            logger.warning(
                "Navixy subscription rejected code=%s description=%s",
                code,
                description,
            )
            raise SourceConnectionError(
                "Navixy subscription rejected "
                f"code={code} description={description}"
            )
        data = event.get("data")
        block = data.get("state_batch") if isinstance(data, dict) else None
        if not isinstance(block, dict) and isinstance(data, dict):
            state = data.get("state")
            block = state if isinstance(state, dict) else None
        counts = _status_counts(
            block,
            self._tracker_ids,
            self._source_to_tracker,
        )
        self.subscription_counts = counts
        if isinstance(data, dict):
            readings = data.get("readings_batch")
            if isinstance(readings, dict) and readings.get("success") is False:
                logger.warning(
                    "Navixy readings_batch subscription was rejected"
                )
        if counts["normal"] > 0:
            return
        logger.warning(
            "Navixy subscription has no deliverable trackers "
            "normal=%s blocked=%s unknown=%s disallowed=%s",
            counts["normal"],
            counts["blocked"],
            counts["unknown"],
            counts["disallowed"],
        )
        raise SourceConnectionError(
            "Navixy subscription has no deliverable trackers "
            f"normal={counts['normal']} blocked={counts['blocked']} "
            f"unknown={counts['unknown']} "
            f"disallowed={counts['disallowed']}"
        )

    def _note_event(self, kind: str) -> None:
        count = self._event_counts.get(kind, 0) + 1
        self._event_counts[kind] = count
        if count == 1:
            logger.info(
                "Navixy websocket event type=%s count=%s",
                kind,
                count,
            )
            return
        logger.debug(
            "Navixy websocket event type=%s count=%s",
            kind,
            count,
        )

    def _note_position(self) -> None:
        self._last_position_at = time.monotonic()
        self._quiet_warned = False

    def _raise_if_stalled(self) -> None:
        now = time.monotonic()
        anchor = self._opened_at
        if self._last_position_at is not None:
            anchor = self._last_position_at
        if now - anchor < self._stall_seconds:
            return
        if self._last_position_at is None:
            logger.warning(
                "Navixy websocket has no positions after %.0fs events=%s",
                self._stall_seconds,
                dict(self._event_counts),
            )
            raise SourceConnectionError(
                "Navixy websocket produced no positions for "
                f"{self._stall_seconds:.0f}s"
            )
        if self._quiet_warned:
            return
        self._quiet_warned = True
        logger.warning(
            "Navixy websocket has no positions for %.0fs events=%s",
            self._stall_seconds,
            dict(self._event_counts),
        )

    async def close(self) -> None:
        pending = self._pending
        self._pending = None
        if pending is not None and not pending.done():
            pending.cancel()
        try:
            await self._socket.close()
        except Exception:
            logger.debug("Navixy websocket close failed", exc_info=True)
