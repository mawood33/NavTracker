import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import UTC, datetime

from app.application.discovery import (
    DEFAULT_DISCOVERY_BACKOFF_CAP_SECONDS,
    DEFAULT_DISCOVERY_BACKOFF_INITIAL_SECONDS,
    discovery_backoff_seconds,
)
from app.application.mode import FailoverPolicy, IngestionMode
from app.application.position_log import log_position
from app.application.publication import (
    PUBLISH_BACKOFF_CAP_SECONDS,
    PUBLISH_BACKOFF_INITIAL_SECONDS,
    PUBLISH_MAX_ATTEMPTS,
    LatestFixRetry,
)
from app.domain.entities import TrackerPosition
from app.domain.errors import SourceConnectionError, TrackerDiscoveryError
from app.domain.ports import (
    MessagePublisher,
    PositionReader,
    PositionStream,
    PositionSubscription,
    TrackerCatalog,
)
from navtracker_common.timeutil import ensure_utc

logger = logging.getLogger(__name__)

_PUBLISHED_TIMESTAMPS_PER_TRACKER = 256


class _ScopeChanged(Exception):
    """The discovered tracker set changed. Reconnect with the new ids."""


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
        catalog: TrackerCatalog | None = None,
        discovery_interval_seconds: float = 0,
        discovery_backoff_initial: float = (
            DEFAULT_DISCOVERY_BACKOFF_INITIAL_SECONDS
        ),
        discovery_backoff_cap: float = DEFAULT_DISCOVERY_BACKOFF_CAP_SECONDS,
        seed_current_state: bool = True,
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
        self._catalog = catalog
        self.discovery_interval_seconds = discovery_interval_seconds
        self._discovery_backoff_initial = discovery_backoff_initial
        self._discovery_backoff_cap = discovery_backoff_cap
        self.seed_current_state = seed_current_state
        self._ordered_ids = tuple(dict.fromkeys(tracker_ids))
        self._scope = set(self._ordered_ids)
        self._uses_discovery = catalog is not None and not self._ordered_ids
        self._scope_changed = asyncio.Event()
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
        discover_task: asyncio.Task[None] | None = None
        try:
            if self._uses_discovery:
                await self._load_discovered_scope()
                if self.discovery_interval_seconds > 0:
                    discover_task = asyncio.create_task(
                        self._rediscover_loop(),
                        name="tracker-discovery",
                    )
            await self._run_modes()
        finally:
            if discover_task is not None:
                discover_task.cancel()
            retry_task.cancel()
            pending = [retry_task]
            if discover_task is not None:
                pending.append(discover_task)
            await asyncio.gather(*pending, return_exceptions=True)

    async def _run_modes(self) -> None:
        if not self._ordered_ids and not self._uses_discovery:
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
            if not self._ordered_ids:
                await self._wait_for_trackers()
                continue
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
            except _ScopeChanged:
                logger.info(
                    "Navixy websocket resubscribing tracker_ids=%s",
                    list(self._ordered_ids),
                )
                continue
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
            except _ScopeChanged:
                continue
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
            await self._publish_isolated(position, "polling continues", "poll")

    async def _consume_session(
        self,
        on_established: Callable[[], None] | None = None,
        on_healthy: Callable[[], None] | None = None,
    ) -> None:
        if not self._ordered_ids:
            raise _ScopeChanged()
        source_map = await self._load_source_map()
        tracker_ids = list(self._ordered_ids)
        if source_map is None:
            session = await self.stream.connect(tracker_ids)
        else:
            session = await self.stream.connect(
                tracker_ids,
                source_to_tracker=source_map,
            )
        try:
            if on_established is not None:
                on_established()
            if self.seed_current_state:
                await self._seed_current()
            started = self.clock.monotonic()
            notified = False
            while True:
                item = await self._next_item(session)
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
                    "websocket",
                )
        finally:
            await session.close()

    async def _load_discovered_scope(self) -> None:
        attempt = 0
        while True:
            try:
                ids = await self._read_catalog()
            except TrackerDiscoveryError as exc:
                attempt += 1
                delay = discovery_backoff_seconds(
                    attempt,
                    initial=self._discovery_backoff_initial,
                    cap=self._discovery_backoff_cap,
                )
                logger.warning(
                    "Navixy tracker discovery failed attempt=%s; "
                    "retrying in %.1fs: %s",
                    attempt,
                    delay,
                    exc,
                )
                await self.clock.sleep(delay)
                continue
            self._replace_scope(ids)
            if not ids:
                logger.warning(
                    "Navixy tracker list is empty; ingestion is idle "
                    "and will not subscribe to every tracker"
                )
                if self.discovery_interval_seconds <= 0:
                    while True:
                        await self.clock.sleep(3600)
                return
            logger.info(
                "Discovered Navixy tracker_ids=%s",
                list(self._ordered_ids),
            )
            return

    async def _rediscover_loop(self) -> None:
        while True:
            await self.clock.sleep(self.discovery_interval_seconds)
            try:
                ids = await self._read_catalog()
            except TrackerDiscoveryError as exc:
                logger.warning(
                    "Navixy tracker re-discovery failed; "
                    "keeping tracker_ids=%s: %s",
                    list(self._ordered_ids),
                    exc,
                )
                continue
            fresh = tuple(dict.fromkeys(ids))
            if fresh == self._ordered_ids:
                logger.debug(
                    "Navixy tracker list unchanged count=%s",
                    len(fresh),
                )
                continue
            added = [tracker_id for tracker_id in fresh if tracker_id not in self._scope]
            removed = [
                tracker_id
                for tracker_id in self._ordered_ids
                if tracker_id not in set(fresh)
            ]
            self._replace_scope(list(fresh))
            logger.info(
                "Navixy tracker list changed added=%s removed=%s "
                "tracker_ids=%s",
                added,
                removed,
                list(self._ordered_ids),
            )
            self._scope_changed.set()

    async def _load_source_map(self) -> dict[int, int] | None:
        """Return source id to tracker id, or None when unavailable.

        None leaves websocket frames on the previous id rule. An empty
        dict means the list was read and no source ids were present.
        """

        catalog = self._catalog
        if catalog is None:
            return None
        load = getattr(catalog, "source_to_tracker", None)
        if not callable(load):
            return None
        try:
            loaded = load()
            if isinstance(loaded, Awaitable):
                loaded = await loaded
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Navixy source id map unavailable: %s", exc)
            return None
        if not isinstance(loaded, dict):
            return {}
        mapping: dict[int, int] = {}
        for source_id, tracker_id in loaded.items():
            if (
                isinstance(source_id, int)
                and not isinstance(source_id, bool)
                and isinstance(tracker_id, int)
                and not isinstance(tracker_id, bool)
            ):
                mapping[source_id] = tracker_id
        return mapping

    async def _seed_current(self) -> None:
        """Publish the latest API state once for each selected tracker.

        Navixy pushes a snapshot at subscribe and then only changes.
        This read is the current fix, not a history backfill.
        """

        logger.info(
            "Seeding current Navixy state tracker_count=%s",
            len(self._ordered_ids),
        )

        async def one(tracker_id: int) -> TrackerPosition | None:
            try:
                return await self.reader.get_position(tracker_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "Navixy current-state seed failed tracker_id=%s",
                    tracker_id,
                )
                return None

        positions = await asyncio.gather(
            *(one(tracker_id) for tracker_id in self._ordered_ids)
        )
        for position in positions:
            if position is None or position.tracker_id not in self._scope:
                continue
            await self._publish_isolated(
                position,
                "current-state seed",
                "seed",
            )

    async def _read_catalog(self) -> list[int]:
        catalog = self._catalog
        if catalog is None:
            raise TrackerDiscoveryError(
                "Navixy tracker list request failed"
            )
        try:
            ids = await catalog.list_tracker_ids()
        except asyncio.CancelledError:
            raise
        except TrackerDiscoveryError:
            raise
        except Exception as exc:
            raise TrackerDiscoveryError(
                "Navixy tracker list request failed"
            ) from exc
        invalid_id = any(
            isinstance(tracker_id, bool) or not isinstance(tracker_id, int)
            for tracker_id in ids
        )
        if invalid_id:
            raise TrackerDiscoveryError(
                "Navixy tracker list did not return integer ids"
            )
        return list(ids)

    def _replace_scope(self, tracker_ids: list[int]) -> None:
        self._ordered_ids = tuple(dict.fromkeys(tracker_ids))
        self._scope = set(self._ordered_ids)

    async def _wait_for_trackers(self) -> None:
        if self.discovery_interval_seconds <= 0:
            while not self._ordered_ids:
                await self.clock.sleep(3600)
            return
        await self._scope_changed.wait()
        self._scope_changed.clear()

    async def _next_item(
        self,
        session: PositionSubscription,
    ) -> TrackerPosition | None:
        receive_task = asyncio.create_task(session.receive())
        change_task = asyncio.create_task(self._scope_changed.wait())
        try:
            done, _pending = await asyncio.wait(
                {receive_task, change_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
        except asyncio.CancelledError:
            receive_task.cancel()
            change_task.cancel()
            await asyncio.gather(
                receive_task,
                change_task,
                return_exceptions=True,
            )
            raise
        for task in (receive_task, change_task):
            if task not in done:
                task.cancel()
        await asyncio.gather(
            receive_task,
            change_task,
            return_exceptions=True,
        )
        if self._scope_changed.is_set():
            self._scope_changed.clear()
            raise _ScopeChanged()
        return receive_task.result()

    async def _publish_isolated(
        self,
        position: TrackerPosition,
        continuing: str,
        source: str,
    ) -> None:
        """Publish without letting a publisher failure escape.

        ``SourceConnectionError`` is raised only by the Navixy stream.
        Anything raised while publishing is contained here so the poll
        loop and the open socket keep going.
        """

        try:
            await self._publish_deduped(position, source)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "Publication handling failed tracker_id=%s; %s",
                position.tracker_id,
                continuing,
            )

    async def _publish_deduped(self, position: TrackerPosition, source: str) -> None:
        async with self._lock:
            previous = self._last_received_at.get(position.tracker_id)
            resolved, timestamp, move_cursor = self._plan_timestamp(position)
            decision = _dedupe_decision(
                timestamp,
                self._published.get(position.tracker_id, set()),
                self._inflight.get(position.tracker_id, set()),
            )
            log_position(
                position,
                source=source,
                compared_to=previous,
                normalized=timestamp,
                decision=decision,
            )
            if move_cursor and timestamp is not None:
                self._last_received_at[position.tracker_id] = timestamp
            if decision != "publish" or timestamp is None:
                return
            inflight = self._inflight.setdefault(position.tracker_id, set())
            inflight.add(timestamp)
        await self._outbox.submit(resolved)

    async def _mark_published(self, position: TrackerPosition) -> None:
        timestamp = _utc_or_none(position.timestamp)
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
        timestamp = _utc_or_none(position.timestamp)
        if timestamp is None:
            return
        async with self._lock:
            inflight = self._inflight.get(position.tracker_id)
            if inflight is not None:
                inflight.discard(timestamp)

    def _resolve_timestamp(self, position: TrackerPosition) -> TrackerPosition:
        resolved, stamp, move_cursor = self._plan_timestamp(position)
        if move_cursor and stamp is not None:
            self._last_received_at[position.tracker_id] = stamp
        return resolved

    def _plan_timestamp(
        self,
        position: TrackerPosition,
    ) -> tuple[TrackerPosition, datetime | None, bool]:
        """Return the UTC fix, its stamp, and whether the cursor moves.

        This does not change supervisor state. A missing timestamp reuses
        the last received stamp, or the clock when the tracker has none.
        """

        if position.timestamp is not None:
            stamp = ensure_utc(position.timestamp)
            current = self._last_received_at.get(position.tracker_id)
            if current is not None:
                current = ensure_utc(current)
            move_cursor = current is None or stamp >= current
            return replace(position, timestamp=stamp), stamp, move_cursor

        last = self._last_received_at.get(position.tracker_id)
        if last is not None:
            stamp = ensure_utc(last)
            return replace(position, timestamp=stamp), stamp, False

        stamped = ensure_utc(self.clock.now())
        return replace(position, timestamp=stamped), stamped, True

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


def _utc_or_none(moment: datetime | None) -> datetime | None:
    if moment is None:
        return None
    return ensure_utc(moment)


def _dedupe_decision(
    timestamp: datetime | None,
    seen: set[datetime],
    inflight: set[datetime],
) -> str:
    if timestamp is None:
        return "drop"
    if timestamp in seen or timestamp in inflight:
        return "duplicate"
    return "publish"
