import asyncio
import logging
from datetime import UTC, datetime

from app.application.mode import FailoverPolicy, IngestionMode
from app.application.supervisor import IngestionSupervisor
from app.domain.entities import TrackerPosition
from app.domain.errors import SourceConnectionError

T1 = datetime(2026, 1, 1, tzinfo=UTC)
T2 = datetime(2026, 1, 1, 0, 1, tzinfo=UTC)


def _position(tracker_id, timestamp, latitude=1.0):
    return TrackerPosition(
        tracker_id=tracker_id,
        latitude=latitude,
        longitude=2.0,
        timestamp=timestamp,
    )


class FakeClock:
    def __init__(self):
        self.monotonic_time = 0.0
        self.sleeps = []
        self.now_calls = 0
        self._now = datetime(2026, 6, 1, tzinfo=UTC)

    def monotonic(self):
        return self.monotonic_time

    def now(self):
        self.now_calls += 1
        return self._now

    def advance(self, seconds):
        self.monotonic_time += seconds

    async def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.monotonic_time += seconds
        await asyncio.sleep(0)


class FakePublisher:
    def __init__(self, fail_times=0):
        self.published = []
        self.attempts = 0
        self.fail_times = fail_times

    async def publish(self, position):
        self.attempts += 1
        if self.attempts <= self.fail_times:
            raise RuntimeError("chat down")
        self.published.append(position)
        return {}


class RecordingReader:
    def __init__(self, position_for):
        self.ids = []
        self.position_for = position_for

    async def get_position(self, tracker_id):
        self.ids.append(tracker_id)
        await asyncio.sleep(0)
        return self.position_for(tracker_id)


class QueueReader:
    def __init__(self):
        self.ids = []
        self.queue = asyncio.Queue()

    async def get_position(self, tracker_id):
        self.ids.append(tracker_id)
        item = await self.queue.get()
        if isinstance(item, Exception):
            raise item
        return item


class ExplodingStream:
    def __init__(self):
        self.called = False
        self.requested = []

    async def connect(self, tracker_ids):
        self.called = True
        self.requested.append(list(tracker_ids))
        raise AssertionError("websocket must not be opened")


class ScriptedSession:
    def __init__(self, events, parked=None):
        self.events = list(events)
        self.parked = parked
        self.closed = False

    async def receive(self):
        if not self.events:
            if self.parked is not None:
                self.parked.set()
            await asyncio.Event().wait()
        event = self.events.pop(0)
        if isinstance(event, BaseException):
            raise event
        return event

    async def close(self):
        self.closed = True


class ScriptedStream:
    def __init__(self, sessions):
        self._sessions = list(sessions)
        self.requested = []
        self.opened = []

    async def connect(self, tracker_ids):
        self.requested.append(list(tracker_ids))
        item = self._sessions.pop(0)
        if isinstance(item, BaseException):
            raise item
        self.opened.append(item)
        return item


class HealthySession:
    def __init__(self, clock, advance, position, parked):
        self.clock = clock
        self.advance = advance
        self.position = position
        self.parked = parked
        self.calls = 0
        self.closed = False

    async def receive(self):
        self.calls += 1
        if self.calls == 1:
            self.clock.advance(self.advance)
            return self.position
        self.parked.set()
        await asyncio.Event().wait()

    async def close(self):
        self.closed = True


def _policy(**overrides):
    values = dict(
        max_attempts=3,
        backoff_initial_seconds=1.0,
        backoff_cap_seconds=30.0,
        probe_interval_seconds=10_000.0,
        healthy_window_seconds=30.0,
    )
    values.update(overrides)
    return FailoverPolicy(**values)


def _supervisor(**kwargs):
    defaults = dict(
        publisher=FakePublisher(),
        mode=IngestionMode.WEBSOCKET,
        poll_interval_seconds=0,
        policy=_policy(),
        clock=FakeClock(),
    )
    defaults.update(kwargs)
    return IngestionSupervisor(**defaults)


async def _until(predicate):
    async def spin():
        while not predicate():
            await asyncio.sleep(0)

    await asyncio.wait_for(spin(), timeout=1)


async def _shutdown(task):
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


def test_poll_mode_requests_only_configured_tracker_ids():
    async def scenario():
        reader = RecordingReader(lambda tracker_id: _position(tracker_id, T1))
        stream = ExplodingStream()
        publisher = FakePublisher()
        supervisor = _supervisor(
            reader=reader,
            stream=stream,
            publisher=publisher,
            tracker_ids=[1, 1, 3],
            mode=IngestionMode.POLL,
        )
        task = asyncio.create_task(supervisor.run())
        await _until(lambda: [item.tracker_id for item in publisher.published] == [1, 3])
        assert stream.called is False
        assert reader.ids[:2] == [1, 3]
        await _shutdown(task)

    asyncio.run(scenario())


def test_poll_mode_keeps_polling_after_a_read_error():
    async def scenario():
        class FlakyReader:
            def __init__(self):
                self.calls = 0

            async def get_position(self, tracker_id):
                self.calls += 1
                await asyncio.sleep(0)
                if self.calls == 1:
                    raise RuntimeError("navixy 500")
                return _position(tracker_id, T1)

        reader = FlakyReader()
        stream = ExplodingStream()
        publisher = FakePublisher()
        supervisor = _supervisor(
            reader=reader,
            stream=stream,
            publisher=publisher,
            tracker_ids=[1],
            mode=IngestionMode.POLL,
        )
        task = asyncio.create_task(supervisor.run())
        await _until(lambda: len(publisher.published) == 1)
        assert stream.called is False
        assert reader.calls >= 2
        await _shutdown(task)

    asyncio.run(scenario())


def test_websocket_filters_tracker_ids_before_publish():
    async def scenario():
        parked = asyncio.Event()
        reader = RecordingReader(lambda tracker_id: _position(tracker_id, T1))
        stream = ScriptedStream(
            [
                ScriptedSession(
                    [
                        _position(1, T1),
                        _position(2, T1),
                        _position(3, T1),
                    ],
                    parked=parked,
                )
            ]
        )
        publisher = FakePublisher()
        supervisor = _supervisor(
            reader=reader,
            stream=stream,
            publisher=publisher,
            tracker_ids=[1, 3],
            policy=_policy(max_attempts=5),
        )
        task = asyncio.create_task(supervisor.run())
        await asyncio.wait_for(parked.wait(), timeout=1)
        assert stream.requested == [[1, 3]]
        assert [item.tracker_id for item in publisher.published] == [1, 3]
        assert reader.ids == []
        await _shutdown(task)

    asyncio.run(scenario())


def test_empty_tracker_ids_do_not_subscribe_or_poll():
    async def scenario():
        reader = RecordingReader(lambda tracker_id: _position(tracker_id, T1))
        stream = ExplodingStream()
        supervisor = _supervisor(
            reader=reader,
            stream=stream,
            publisher=FakePublisher(),
            tracker_ids=[],
            mode=IngestionMode.WEBSOCKET,
        )
        task = asyncio.create_task(supervisor.run())
        for _ in range(5):
            await asyncio.sleep(0)
        assert reader.ids == []
        assert stream.called is False
        await _shutdown(task)

    asyncio.run(scenario())


def test_failover_threshold_switches_to_poll(caplog):
    async def scenario():
        caplog.set_level(logging.WARNING)
        clock = FakeClock()
        reader = RecordingReader(lambda tracker_id: _position(tracker_id, T1))
        stream = ScriptedStream(
            [
                SourceConnectionError("down-1"),
                SourceConnectionError("down-2"),
                SourceConnectionError("down-3"),
                SourceConnectionError("down-4"),
                ScriptedSession([]),
            ]
        )
        supervisor = _supervisor(
            reader=reader,
            stream=stream,
            publisher=FakePublisher(),
            tracker_ids=[7, 8],
            policy=_policy(
                max_attempts=4,
                backoff_initial_seconds=1.0,
                backoff_cap_seconds=3.0,
            ),
            clock=clock,
        )
        task = asyncio.create_task(supervisor.run())
        await _until(lambda: len(reader.ids) >= 2)
        assert stream.requested[:4] == [[7, 8], [7, 8], [7, 8], [7, 8]]
        assert clock.sleeps[:3] == [1.0, 2.0, 3.0]
        assert reader.ids[:2] == [7, 8]
        assert "Ingestion mode switch: websocket -> poll" in caplog.text
        assert "poll -> websocket" not in caplog.text
        await _shutdown(task)

    asyncio.run(scenario())


def test_publish_errors_do_not_count_toward_failover():
    async def scenario():
        parked = asyncio.Event()
        positions = [_position(4, T1, latitude=index) for index in range(3)]
        reader = RecordingReader(lambda tracker_id: _position(tracker_id, T2))
        stream = ScriptedStream(
            [
                ScriptedSession(
                    [*positions, SourceConnectionError("socket dropped")]
                ),
                ScriptedSession([], parked=parked),
            ]
        )
        publisher = FakePublisher(fail_times=10)
        supervisor = _supervisor(
            reader=reader,
            stream=stream,
            publisher=publisher,
            tracker_ids=[4],
            policy=_policy(max_attempts=2),
            publish_attempts=1,
        )
        task = asyncio.create_task(supervisor.run())
        await asyncio.wait_for(parked.wait(), timeout=1)
        assert publisher.attempts == 3
        assert publisher.published == []
        assert reader.ids == []
        assert stream.requested == [[4], [4]]
        await _shutdown(task)

    asyncio.run(scenario())


def test_successful_session_resets_failure_streak():
    async def scenario():
        parked = asyncio.Event()
        reader = RecordingReader(lambda tracker_id: _position(tracker_id, T1))
        stream = ScriptedStream(
            [
                SourceConnectionError("startup"),
                ScriptedSession([SourceConnectionError("runtime")]),
                ScriptedSession([], parked=parked),
            ]
        )
        supervisor = _supervisor(
            reader=reader,
            stream=stream,
            publisher=FakePublisher(),
            tracker_ids=[4],
            policy=_policy(max_attempts=2),
        )
        task = asyncio.create_task(supervisor.run())
        await asyncio.wait_for(parked.wait(), timeout=1)
        assert reader.ids == []
        assert stream.requested == [[4], [4], [4]]
        await _shutdown(task)

    asyncio.run(scenario())


def test_switch_back_after_healthy_window(caplog):
    async def scenario():
        caplog.set_level(logging.WARNING)
        clock = FakeClock()
        parked = asyncio.Event()
        reader = RecordingReader(lambda tracker_id: _position(tracker_id, T1))
        stream = ScriptedStream(
            [
                SourceConnectionError("startup"),
                HealthySession(
                    clock,
                    advance=10,
                    position=_position(4, T2),
                    parked=parked,
                ),
            ]
        )
        publisher = FakePublisher()
        supervisor = _supervisor(
            reader=reader,
            stream=stream,
            publisher=publisher,
            tracker_ids=[4],
            policy=_policy(
                max_attempts=1,
                probe_interval_seconds=0,
                healthy_window_seconds=10,
            ),
            clock=clock,
            poll_interval_seconds=0,
        )
        task = asyncio.create_task(supervisor.run())
        await asyncio.wait_for(parked.wait(), timeout=1)
        for _ in range(5):
            await asyncio.sleep(0)
        stable = len(reader.ids)
        assert stable >= 1
        for _ in range(5):
            await asyncio.sleep(0)
        assert len(reader.ids) == stable
        assert stream.requested == [[4], [4]]
        assert any(item.timestamp == T2 for item in publisher.published)
        text = caplog.text
        assert text.index("websocket -> poll") < text.index("poll -> websocket")
        assert "stayed healthy for 10s" in text
        await _shutdown(task)

    asyncio.run(scenario())


def test_dedupes_tracker_id_and_timestamp_across_failover(caplog):
    async def scenario():
        caplog.set_level(logging.INFO)
        clock = FakeClock()
        reader = QueueReader()
        stream = ScriptedStream(
            [
                ScriptedSession(
                    [_position(4, T1), SourceConnectionError("drop")]
                ),
                ScriptedSession([]),
            ]
        )
        publisher = FakePublisher()
        supervisor = _supervisor(
            reader=reader,
            stream=stream,
            publisher=publisher,
            tracker_ids=[4],
            policy=_policy(max_attempts=1, probe_interval_seconds=0),
            clock=clock,
        )
        task = asyncio.create_task(supervisor.run())
        await _until(lambda: len(publisher.published) == 1)
        await _until(lambda: len(reader.ids) >= 1)
        reader.queue.put_nowait(_position(4, T1))
        await _until(lambda: len(reader.ids) >= 2)
        assert len(publisher.published) == 1
        assert clock.now_calls == 0

        reader.queue.put_nowait(_position(4, T2))
        await _until(lambda: len(publisher.published) == 2)
        reader.queue.put_nowait(_position(4, None))
        await _until(lambda: len(reader.ids) >= 4)
        assert [item.timestamp for item in publisher.published] == [T1, T2]
        assert clock.now_calls == 0
        assert "Polling resumes from last received fix" in caplog.text
        assert T1.isoformat() in caplog.text
        await _shutdown(task)

    asyncio.run(scenario())


def test_websocket_publish_failure_retries_without_reconnecting(caplog):
    async def scenario():
        caplog.set_level(logging.WARNING)
        parked = asyncio.Event()
        reader = RecordingReader(lambda tracker_id: _position(tracker_id, T1))
        session = ScriptedSession([_position(4, T1)], parked=parked)
        stream = ScriptedStream([session])
        publisher = FakePublisher(fail_times=1)
        supervisor = _supervisor(
            reader=reader,
            stream=stream,
            publisher=publisher,
            tracker_ids=[4],
            clock=FakeClock(),
            publish_attempts=3,
            publish_backoff_initial=0,
            publish_backoff_cap=0,
        )
        task = asyncio.create_task(supervisor.run())
        await _until(
            lambda: len(publisher.published) == 1 and parked.is_set()
        )
        assert publisher.attempts == 2
        assert stream.requested == [[4]]
        assert session.closed is False
        assert reader.ids == []
        assert "Navixy websocket failure" not in caplog.text
        await _shutdown(task)

    asyncio.run(scenario())


def test_websocket_publish_failure_is_dropped_without_failover(caplog):
    async def scenario():
        caplog.set_level(logging.ERROR)
        parked = asyncio.Event()
        reader = RecordingReader(lambda tracker_id: _position(tracker_id, T2))
        session = ScriptedSession([_position(4, T1)], parked=parked)
        stream = ScriptedStream([session])
        publisher = FakePublisher(fail_times=10)
        supervisor = _supervisor(
            reader=reader,
            stream=stream,
            publisher=publisher,
            tracker_ids=[4],
            clock=FakeClock(),
            publish_attempts=3,
            publish_backoff_initial=0,
            publish_backoff_cap=0,
            policy=_policy(max_attempts=2),
        )
        task = asyncio.create_task(supervisor.run())
        await asyncio.wait_for(parked.wait(), timeout=1)
        await _until(lambda: "Dropping unpublished fix" in caplog.text)
        assert publisher.attempts == 3
        assert publisher.published == []
        assert stream.requested == [[4]]
        assert session.closed is False
        assert reader.ids == []
        assert "tracker_id=4" in caplog.text
        await _shutdown(task)

    asyncio.run(scenario())


def test_poll_publish_failure_does_not_open_the_websocket(caplog):
    async def scenario():
        caplog.set_level(logging.ERROR)
        reader = RecordingReader(lambda tracker_id: _position(tracker_id, T1))
        stream = ExplodingStream()
        publisher = FakePublisher(fail_times=10)
        supervisor = _supervisor(
            reader=reader,
            stream=stream,
            publisher=publisher,
            tracker_ids=[1, 2],
            mode=IngestionMode.POLL,
            clock=FakeClock(),
            publish_attempts=1,
        )
        task = asyncio.create_task(supervisor.run())
        await _until(lambda: len(reader.ids) >= 3)
        assert stream.called is False
        assert reader.ids[:2] == [1, 2]
        assert "Dropping unpublished fix" in caplog.text
        assert publisher.published == []
        await _shutdown(task)

    asyncio.run(scenario())
