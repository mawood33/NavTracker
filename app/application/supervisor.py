import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime

from app.application.mode import FailoverPolicy, IngestionMode
from app.application.publication import (
    PUBLISH_BACKOFF_CAP_SECONDS,
    PUBLISH_BACKOFF_INITIAL_SECONDS,
    PUBLISH_MAX_ATTEMPTS,
    LatestFixRetry,
)
from app.domain.entities import TrackerPosition
from app.domain.errors import SourceConnectionError
from app.domain.ports import MessagePublisher, PositionReader, PositionStream

logger = logging.getLogger(__name__)

_PUBLISHED_TIMESTAMPS_PER_TRACKER = 256


class SystemClock:
    def monotonic(self) -> float:
        return time.monotonic()

    def now(self) -> datetime:
        return datetime.now(UTC)

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


class IngestionSupervisor:
    """Owns the poll reader and the websocket stream.

    NAVIXY_MODE=poll uses the reader only. NAVIXY_MODE=websocket streams,
    retries the socket with exponential backoff, and after
    ``policy.max_attempts`` consecutive socket failures polls until a
    background probe stays up for ``policy.healthy_window_seconds``.

    Publish errors are retried off the read loop and do not count as
    socket failures. The same tracker id + timestamp is published once.
    A fix with no provider timestamp keeps the last received timestamp
    for that tracker instead of the wall clock, so a mode switch does
    not invent a new fix.
    """

    def __init__(
        self,
        reader: PositionReader,
        stream: PositionStream,
        publisher: MessagePublisher,
        tracker_ids: list[int],
        mode: IngestionMode,
        poll_interval_seconds: float,
        policy: FailoverPolicy,
        clock: SystemClock | None = None,
        publish_attempts: int = PUBLISH_MAX_ATTEMPTS,
        publish_backoff_initial: float = PUBLISH_BACKOFF_INITIAL_SECONDS,
        publish_backoff_cap: float = PUBLISH_BACKOFF_CAP_SECONDS,
    ):
        self.reader = reader
        self.stream = stream
        self.publisher = publisher
        self.mode = mode
        self.poll_interval_seconds = poll_interval_seconds
        self.policy = policy
        self.clock = clock or SystemClock()
        self.publish_attempts = publish_attempts
        self.publish_backoff_initial = publish_backoff_initial
        self.publish_backoff_cap = publish_backoff_cap
        self._ordered_ids = tuple(dict.fromkeys(tracker_ids))
        self._scope = set(self._ordered_ids)
        self._lock = asyncio.Lock()
        self._last_received_at: dict[int, datetime] = {}
        self._published: dict[int, set[datetime]] = {}
        self._inflight: dict[int, set[datetime]] = {}
        self._outbox = LatestFixRetry(
            publisher=publisher,
            clock=self.clock,
            on_success=self._mark_published,
            on_abandon=self._forget_inflight,
            max_attempts=publish_attempts,
            backoff_initial=publish_backoff_initial,
            backoff_cap=publish_backoff_cap,
        )

    async def run(self) -> None:
        retry_task = asyncio.create_task(
            self._outbox.run(),
            name="publish-retry",
        )
        try:
            await self._run_modes()
        finally:
            retry_task.cancel()
            await asyncio.gather(retry_task, return_exceptions=True)

    async def _run_modes(self) -> None:
        if not self._ordered_ids:
            logger.warning(
                "No tracker ids configured; Navixy ingestion is idle "
                "and will not subscribe to every tracker"
            )
            while True:
                await self.clock.sleep(3600)
            return

        if self.mode is IngestionMode.POLL:
            logger.info(
                "NAVIXY_MODE=poll; API polling only, "
                "websocket failover disabled"
            )
            await self._poll_forever()
            return

        logger.info(
            "NAVIXY_MODE=websocket; failover to API polling after %s "
            "consecutive Navixy socket failures",
            self.policy.max_attempts,
        )
        failures = 0
        while True:
            if failures >= self.policy.max_attempts:
                logger.warning(
                    "Ingestion mode switch: websocket -> poll after %s "
                    "consecutive Navixy socket failures",
                    self.policy.max_attempts,
                )
                self._log_resume_cursor()
                await self._poll_until_websocket_recovers()
                failures = 1
                logger.warning(
                    "Navixy websocket failure %s/%s: recovered session ended",
                    failures,
                    self.policy.max_attempts,
                )
                if failures >= self.policy.max_attempts:
                    continue
                delay = self.policy.backoff_seconds(failures)
                logger.info(
                    "Recovered Navixy websocket ended; retrying in %.1fs",
                    delay,
                )
                await self.clock.sleep(delay)
                continue

            try:
                def established() -> None:
                    nonlocal failures
                    failures = 0

                await self._consume_session(on_established=established)
            except SourceConnectionError as exc:
                failures += 1
                logger.warning(
                    "Navixy websocket failure %s/%s: %s",
                    failures,
                    self.policy.max_attempts,
                    exc,
                )
                if failures >= self.policy.max_attempts:
                    continue
                delay = self.policy.backoff_seconds(failures)
                logger.info("Retrying Navixy websocket in %.1fs", delay)
                await self.clock.sleep(delay)

    async def _poll_forever(self) -> None:
        await self._poll_loop(asyncio.Event())

    async def _poll_until_websocket_recovers(self) -> None:
        stop_poll = asyncio.Event()

        def on_healthy() -> None:
            logger.warning(
                "Ingestion mode switch: poll -> websocket; "
                "Navixy socket stayed healthy for %.0fs",
                self.policy.healthy_window_seconds,
            )
            stop_poll.set()

        poll_task = asyncio.create_task(self._poll_loop(stop_poll))
        probe_task = asyncio.create_task(
            self._probe_loop(stop_poll, on_healthy)
        )
        try:
            await probe_task
        finally:
            stop_poll.set()
            poll_task.cancel()
            if not probe_task.done():
                probe_task.cancel()
            await asyncio.gather(
                poll_task,
                probe_task,
                return_exceptions=True,
            )

    async def _probe_loop(
        self,
        stop_poll: asyncio.Event,
        on_healthy: Callable[[], None],
    ) -> None:
        while not stop_poll.is_set():
            await self.clock.sleep(self.policy.probe_interval_seconds)
            if stop_poll.is_set():
                return
            logger.info("Probing Navixy websocket while polling")
            try:
                await self._consume_session(on_healthy=on_healthy)
            except SourceConnectionError as exc:
                if stop_poll.is_set():
                    logger.warning(
                        "Navixy websocket ended after recovery: %s",
                        exc,
                    )
                    return
                logger.info(
                    "Navixy websocket probe failed; staying on poll: %s",
                    exc,
                )
                continue
            if stop_poll.is_set():
                return

    async def _poll_loop(self, stop_poll: asyncio.Event) -> None:
        while not stop_poll.is_set():
            await self._poll_once()
            if stop_poll.is_set():
                return
            await self.clock.sleep(self.poll_interval_seconds)

    async def _poll_once(self) -> None:
        for tracker_id in self._ordered_ids:
            try:
                position = await self.reader.get_position(tracker_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "Polling failed for tracker_id=%s",
                    tracker_id,
                )
                continue
            if position.tracker_id not in self._scope:
                continue
            await self._publish_isolated(position, "polling continues")

    async def _consume_session(
        self,
        on_established: Callable[[], None] | None = None,
        on_healthy: Callable[[], None] | None = None,
    ) -> None:
        session = await self.stream.connect(list(self._ordered_ids))
        try:
            if on_established is not None:
                on_established()
            started = self.clock.monotonic()
            notified = False
            while True:
                item = await session.receive()
                if (
                    on_healthy is not None
                    and not notified
                    and self.clock.monotonic() - started
                    >= self.policy.healthy_window_seconds
                ):
                    notified = True
                    on_healthy()
                if item is None or item.tracker_id not in self._scope:
                    continue
                await self._publish_isolated(
                    item,
                    "Navixy socket stays open",
                )
        finally:
            await session.close()

    async def _publish_isolated(
        self,
        position: TrackerPosition,
        continuing: str,
    ) -> None:
        """Publish without letting a publisher failure escape.

        ``SourceConnectionError`` is raised only by the Navixy stream.
        Anything raised while publishing is contained here so the poll
        loop and the open socket keep going.
        """

        try:
            await self._publish_deduped(position)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "Publication handling failed tracker_id=%s; %s",
                position.tracker_id,
                continuing,
            )

    async def _publish_deduped(self, position: TrackerPosition) -> None:
        async with self._lock:
            resolved = self._resolve_timestamp(position)
            timestamp = resolved.timestamp
            if timestamp is None:
                return
            seen = self._published.setdefault(resolved.tracker_id, set())
            inflight = self._inflight.setdefault(resolved.tracker_id, set())
            if timestamp in seen or timestamp in inflight:
                return
            inflight.add(timestamp)
        await self._outbox.submit(resolved)

    async def _mark_published(self, position: TrackerPosition) -> None:
        timestamp = position.timestamp
        if timestamp is None:
            return
        async with self._lock:
            seen = self._published.setdefault(position.tracker_id, set())
            seen.add(timestamp)
            inflight = self._inflight.get(position.tracker_id)
            if inflight is not None:
                inflight.discard(timestamp)
            if len(seen) > _PUBLISHED_TIMESTAMPS_PER_TRACKER:
                self._published[position.tracker_id] = {timestamp}

    async def _forget_inflight(self, position: TrackerPosition) -> None:
        timestamp = position.timestamp
        if timestamp is None:
            return
        async with self._lock:
            inflight = self._inflight.get(position.tracker_id)
            if inflight is not None:
                inflight.discard(timestamp)

    def _resolve_timestamp(self, position: TrackerPosition) -> TrackerPosition:
        if position.timestamp is not None:
            current = self._last_received_at.get(position.tracker_id)
            if current is None or position.timestamp >= current:
                self._last_received_at[position.tracker_id] = (
                    position.timestamp
                )
            return position

        last = self._last_received_at.get(position.tracker_id)
        if last is not None:
            return replace(position, timestamp=last)

        stamped = self.clock.now()
        self._last_received_at[position.tracker_id] = stamped
        return replace(position, timestamp=stamped)

    def _log_resume_cursor(self) -> None:
        if not self._last_received_at:
            logger.info(
                "Polling starts with no prior fix; a missing Navixy "
                "timestamp uses the local clock only for the first fix "
                "of each tracker"
            )
            return
        rendered = ", ".join(
            f"{tracker_id}@{stamp.isoformat()}"
            for tracker_id, stamp in sorted(self._last_received_at.items())
        )
        logger.info(
            "Polling resumes from last received fix: %s",
            rendered,
        )
