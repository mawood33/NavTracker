import asyncio
import logging
from datetime import UTC, datetime

import httpx
from app.application.publication import LatestFixRetry, publish_backoff
from app.domain.entities import TrackerPosition
from app.domain.errors import PublicationError
from app.infrastructure.chatsurfer import ChatSurferApiClient

T1 = datetime(2026, 1, 1, tzinfo=UTC)
T2 = datetime(2026, 1, 1, 0, 1, tzinfo=UTC)


class Clock:
    async def sleep(self, seconds):
        await asyncio.sleep(0)


class Publisher:
    def __init__(self, fail_times):
        self.fail_times = fail_times
        self.attempts = 0
        self.published = []

    async def publish(self, position):
        self.attempts += 1
        if self.attempts <= self.fail_times:
            raise PublicationError(f"down {self.attempts}")
        self.published.append(position)
        return {}


def _position(timestamp, tracker_id=4):
    return TrackerPosition(
        tracker_id=tracker_id,
        latitude=1.0,
        longitude=2.0,
        timestamp=timestamp,
    )


def test_backoff_grows_and_caps():
    assert publish_backoff(1, 0.5, 2.0) == 0.5
    assert publish_backoff(2, 0.5, 2.0) == 1.0
    assert publish_backoff(3, 0.5, 2.0) == 2.0
    assert publish_backoff(4, 0.5, 2.0) == 2.0


def test_retry_then_success_publishes_once():
    async def scenario():
        publisher = Publisher(fail_times=1)
        succeeded = []

        async def on_success(position):
            succeeded.append(position)

        async def on_abandon(position):
            raise AssertionError(position)

        outbox = LatestFixRetry(
            publisher,
            Clock(),
            on_success,
            on_abandon,
            max_attempts=3,
            backoff_initial=0,
            backoff_cap=0,
        )
        task = asyncio.create_task(outbox.run())
        await outbox.submit(_position(T1))
        for _ in range(20):
            if succeeded:
                break
            await asyncio.sleep(0)
        assert publisher.attempts == 2
        assert [item.timestamp for item in succeeded] == [T1]
        assert publisher.published[0].timestamp == T1
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())


def test_retry_budget_then_drop_logs_the_fix(caplog):
    async def scenario():
        caplog.set_level(logging.ERROR)
        publisher = Publisher(fail_times=10)
        abandoned = []

        async def on_success(position):
            raise AssertionError(position)

        async def on_abandon(position):
            abandoned.append(position)

        outbox = LatestFixRetry(
            publisher,
            Clock(),
            on_success,
            on_abandon,
            max_attempts=3,
            backoff_initial=0,
            backoff_cap=0,
        )
        task = asyncio.create_task(outbox.run())
        await outbox.submit(_position(T1))
        for _ in range(30):
            if abandoned:
                break
            await asyncio.sleep(0)
        assert publisher.attempts == 3
        assert [item.timestamp for item in abandoned] == [T1]
        assert "Dropping unpublished fix after 3 attempts" in caplog.text
        assert "tracker_id=4" in caplog.text
        assert "2026-01-01T00:00:00Z" in caplog.text
        assert "PublicationError" in caplog.text
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())


def test_newer_fix_replaces_pending_retry():
    async def scenario():
        publisher = Publisher(fail_times=100)
        succeeded = []
        abandoned = []

        async def on_success(position):
            succeeded.append(position.timestamp)

        async def on_abandon(position):
            abandoned.append(position.timestamp)

        outbox = LatestFixRetry(
            publisher,
            Clock(),
            on_success,
            on_abandon,
            max_attempts=5,
            backoff_initial=0,
            backoff_cap=0,
        )
        task = asyncio.create_task(outbox.run())
        await outbox.submit(_position(T1))
        await outbox.submit(_position(T2))
        publisher.fail_times = publisher.attempts
        for _ in range(40):
            if T2 in succeeded:
                break
            await asyncio.sleep(0)
        assert T1 in abandoned
        assert succeeded == [T2]
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())


def test_naive_and_aware_timestamps_compare_without_error():
    async def scenario():
        publisher = Publisher(fail_times=10)
        abandoned = []

        async def on_success(position):
            raise AssertionError(position)

        async def on_abandon(position):
            abandoned.append(position.timestamp)

        outbox = LatestFixRetry(
            publisher,
            Clock(),
            on_success,
            on_abandon,
            max_attempts=3,
            backoff_initial=0,
            backoff_cap=0,
        )
        await outbox.submit(_position(datetime(2026, 1, 1)))
        await outbox.submit(_position(datetime(2025, 6, 1, tzinfo=UTC)))
        await outbox.submit(_position(datetime(2026, 6, 1)))
        assert abandoned[0] == datetime(2025, 6, 1, tzinfo=UTC)
        assert abandoned[1] == datetime(2026, 1, 1, tzinfo=UTC)
        pending = outbox._pending[4].position.timestamp
        assert pending == datetime(2026, 6, 1, tzinfo=UTC)

    asyncio.run(scenario())


def test_chatsurfer_http_error_is_a_publication_error():
    response = httpx.Response(503, json={"ok": False})
    try:
        ChatSurferApiClient._handle_response(response)
    except PublicationError as exc:
        message = str(exc)
    else:
        raise AssertionError("expected PublicationError")
    assert "503" in message
