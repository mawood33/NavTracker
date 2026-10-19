"""Per-room ChatSurfer read status and backoff.

A 404, 401, or 403 is logged once. Later attempts with the same status
stay quiet until the log interval. 5xx responses and timeouts use the
same backoff and a warning.
"""

import logging
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass

logger = logging.getLogger(__name__)

BACKOFF_CAP_SECONDS = 300.0
LOG_INTERVAL_SECONDS = 300.0


@dataclass
class _RoomRead:
    status: str = "pending"
    failures: int = 0
    next_attempt: float = 0.0
    last_logged: str = ""
    last_log_at: float = -1e9


class ChatReadHealth:
    """Backoff and the status string exposed on ``/health``."""

    def __init__(self, clock: Callable[[], float] | None = None) -> None:
        self._clock = clock or time.monotonic
        self._rooms: dict[str, _RoomRead] = {}

    def snapshot(self, rooms: Sequence[str], *, enabled: bool) -> dict[str, str]:
        if not enabled:
            return {room: "disabled" for room in rooms}
        return {
            room: self._rooms[room].status if room in self._rooms else "pending"
            for room in rooms
        }

    def allow(self, room: str) -> bool:
        current = self._rooms.get(room)
        if current is None:
            return True
        return self._clock() >= current.next_attempt

    def record_empty(self, room: str) -> None:
        self._rooms[room] = _RoomRead(status="empty_or_missing")
        logger.debug("ChatSurfer room empty or missing room=%s", room)

    def record_ok(self, room: str) -> None:
        previous = self._rooms.get(room)
        self._rooms[room] = _RoomRead(status="ok")
        if previous is not None and previous.status != "ok":
            logger.info("ChatSurfer read recovered room=%s", room)

    def record_error(
        self,
        room: str,
        *,
        status_code: int | None,
        path: str,
        kind: str,
        initial: float,
    ) -> None:
        label = _label(status_code, kind)
        now = self._clock()
        current = self._rooms.get(room, _RoomRead())
        failures = current.failures + 1
        base = initial if initial > 0 else 1.0
        delay = min(BACKOFF_CAP_SECONDS, base * (2 ** (failures - 1)))
        changed = current.last_logged != label
        due = changed or (now - current.last_log_at) >= LOG_INTERVAL_SECONDS
        logged = current.last_logged
        logged_at = current.last_log_at
        if due:
            _log_failure(status_code, kind, path, room)
            logged = label
            logged_at = now
        self._rooms[room] = _RoomRead(
            status=label,
            failures=failures,
            next_attempt=now + delay,
            last_logged=logged,
            last_log_at=logged_at,
        )


def _label(status_code: int | None, kind: str) -> str:
    if status_code is not None:
        return f"error:{status_code}"
    if kind == "timeout":
        return "error:timeout"
    return "error:unavailable"


def _log_failure(status_code: int | None, kind: str, path: str, room: str) -> None:
    if status_code is not None:
        shown: object = status_code
    elif kind == "timeout":
        shown = "timeout"
    else:
        shown = "unavailable"
    if status_code in {401, 403, 404}:
        logger.error(
            "ChatSurfer read failed status=%s path=%s room=%s",
            shown,
            path,
            room,
        )
        return
    logger.warning(
        "ChatSurfer read failed status=%s path=%s room=%s",
        shown,
        path,
        room,
    )
