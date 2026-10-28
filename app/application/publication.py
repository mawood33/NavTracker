import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Protocol

from app.application.messages import to_outbound_message
from app.domain.entities import TrackerPosition
from app.domain.ports import MessagePublisher
from navtracker_common.timeutil import ensure_utc, format_zulu

logger = logging.getLogger(__name__)

# One immediate attempt plus two retries. Short delays so a blip is
# recovered, and the cap stops a dead publisher from backing off forever
# inside a single fix's budget. The read loop does not wait on these.
PUBLISH_MAX_ATTEMPTS = 3
PUBLISH_BACKOFF_INITIAL_SECONDS = 0.5
PUBLISH_BACKOFF_CAP_SECONDS = 2.0


class _Clock(Protocol):
    async def sleep(self, seconds: float) -> None:
        """Pause the retry loop."""


def publish_backoff(failed_attempts: int, initial: float, cap: float) -> float:
    power = max(failed_attempts, 1) - 1
    delay = initial * (1 << power)
    if delay < cap:
        return delay
    return cap


def _is_older_than_pending(position: TrackerPosition, current: object) -> bool:
    pending = current
    if not isinstance(pending, _Pending):
        return False
    left = _utc_or_none(position.timestamp)
    right = _utc_or_none(pending.position.timestamp)
    if left is None or right is None:
        return False
    return left < right


def _aware(position: TrackerPosition) -> TrackerPosition:
    stamp = _utc_or_none(position.timestamp)
    if stamp is None or stamp == position.timestamp:
        return position
    return replace(position, timestamp=stamp)


def _utc_or_none(moment: datetime | None) -> datetime | None:
    if moment is None:
        return None
    return ensure_utc(moment)


def _stamp(position: TrackerPosition) -> str:
    if position.timestamp is None:
        return "none"
    return format_zulu(position.timestamp)


@dataclass
class _Pending:
    position: TrackerPosition
    attempts: int
    last_error: BaseException | None = None


class LatestFixRetry:
    """Publish a fix, and on failure keep only the latest one per tracker.

    The caller returns as soon as the first attempt finishes. Further
    attempts run in ``run`` so a ChatSurfer outage cannot stall the
    Navixy read loop or look like a socket failure.

    An unbounded queue is a poor fit: while the publisher is down, the
    useful payload is the newest fix, and a backlog would burst through
    the rate limiter after recovery. After ``max_attempts`` the pending
    fix is logged and dropped.
    """

    def __init__(
        self,
        publisher: MessagePublisher,
        clock: _Clock,
        on_success: Callable[[TrackerPosition], Awaitable[None]],
        on_abandon: Callable[[TrackerPosition], Awaitable[None]],
        max_attempts: int = PUBLISH_MAX_ATTEMPTS,
        backoff_initial: float = PUBLISH_BACKOFF_INITIAL_SECONDS,
        backoff_cap: float = PUBLISH_BACKOFF_CAP_SECONDS,
    ):
        self.publisher = publisher
        self.clock = clock
        self._on_success = on_success
        self._on_abandon = on_abandon
        self.max_attempts = max_attempts
        self.backoff_initial = backoff_initial
        self.backoff_cap = backoff_cap
        self._pending: dict[int, _Pending] = {}
        self._lock = asyncio.Lock()
        self._wake = asyncio.Event()

    async def submit(self, position: TrackerPosition) -> None:
        position = _aware(position)
        error = await self._attempt(position)
        if error is None:
            await self._on_success(position)
            return
        if self.max_attempts <= 1:
            await self._drop(position, 1, error)
            return

        abandoned: TrackerPosition | None = None
        queued = False
        async with self._lock:
            current = self._pending.get(position.tracker_id)
            if _is_older_than_pending(position, current):
                abandoned = position
            else:
                if current is not None and _utc_or_none(
                    current.position.timestamp
                ) != _utc_or_none(position.timestamp):
                    abandoned = current.position
                self._pending[position.tracker_id] = _Pending(
                    position,
                    1,
                    error,
                )
                queued = True
        if abandoned is not None and abandoned is not position:
            logger.info(
                "Replacing unpublished fix tracker_id=%s timestamp=%s "
                "with timestamp=%s",
                position.tracker_id,
                _stamp(abandoned),
                _stamp(position),
            )
        if abandoned is not None:
            await self._on_abandon(abandoned)
        if queued:
            self._wake.set()

    async def run(self) -> None:
        while True:
            await self._wake.wait()
            self._wake.clear()
            await self._retry_pending()

    async def _retry_pending(self) -> None:
        while True:
            async with self._lock:
                if not self._pending:
                    return
                tracker_id, pending = next(iter(self._pending.items()))
                position = pending.position
                delay = publish_backoff(
                    pending.attempts,
                    self.backoff_initial,
                    self.backoff_cap,
                )
            await self.clock.sleep(delay)
            error = await self._attempt(position)
            succeeded: TrackerPosition | None = None
            dropped: tuple[TrackerPosition, int, BaseException] | None = None
            async with self._lock:
                current = self._pending.get(tracker_id)
                if current is None or _utc_or_none(
                    current.position.timestamp
                ) != _utc_or_none(position.timestamp):
                    continue
                if error is None:
                    del self._pending[tracker_id]
                    succeeded = position
                else:
                    current.attempts += 1
                    current.last_error = error
                    if current.attempts >= self.max_attempts:
                        del self._pending[tracker_id]
                        dropped = (position, current.attempts, error)
            if succeeded is not None:
                await self._on_success(succeeded)
            elif dropped is not None:
                await self._drop(*dropped)

    async def _attempt(self, position: TrackerPosition) -> BaseException | None:
        try:
            await self.publisher.publish(to_outbound_message(position))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "Publication failed tracker_id=%s timestamp=%s: %s",
                position.tracker_id,
                _stamp(position),
                exc,
            )
            return exc
        return None

    async def _drop(
        self,
        position: TrackerPosition,
        attempts: int,
        error: BaseException,
    ) -> None:
        logger.error(
            "Dropping unpublished fix after %s attempts "
            "tracker_id=%s timestamp=%s",
            attempts,
            position.tracker_id,
            _stamp(position),
            exc_info=error,
        )
        await self._on_abandon(position)
