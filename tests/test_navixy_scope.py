import asyncio
import json

import pytest

from app.domain.errors import SourceConnectionError
from app.infrastructure.navixy_ws import (
    NavixySubscription,
    NavixyWebSocketClient,
    build_subscribe_message,
)


def test_subscribe_message_selects_tracker_ids_only():
    message = build_subscribe_message("hash-1", [9, 8])

    assert message["action"] == "subscribe"
    assert message["hash"] == "hash-1"
    assert "trackers" not in message
    assert message["requests"][0]["target"] == {
        "type": "selected",
        "tracker_ids": [9, 8],
    }
    assert message["requests"][1]["type"] == "readings_batch"
    assert message["requests"][1]["target"] == {
        "type": "selected",
        "tracker_ids": [9, 8],
    }
    assert '"all"' not in json.dumps(message)


class _Socket:
    def __init__(self, frames):
        self.frames = list(frames)
        self.closed = False

    async def recv(self):
        if not self.frames:
            await asyncio.Event().wait()
        return self.frames.pop(0)

    async def close(self):
        self.closed = True


def _frame(tracker_id, timestamp="2026-01-01T00:00:00Z"):
    body = {"tracker_id": tracker_id, "lat": 1.5, "lng": 2.5}
    if timestamp is not None:
        body["timestamp"] = timestamp
    return json.dumps(body)


def test_subscription_drops_trackers_outside_scope_and_keeps_missing_time():
    socket = _Socket(
        [
            _frame(1),
            _frame(99),
            _frame(3, timestamp=None),
            "not-json",
        ]
    )
    subscription = NavixySubscription(socket, {1, 3}, idle_timeout=1)

    async def collect():
        found = []
        for _ in range(4):
            item = await subscription.receive()
            if item is not None:
                found.append(item)
        await subscription.close()
        return found

    found = asyncio.run(collect())
    assert [item.tracker_id for item in found] == [1, 3]
    assert found[0].timestamp is not None
    assert found[1].timestamp is None
    assert socket.closed is True


def test_connect_sends_selected_subscription(monkeypatch):
    sent = []

    class Socket:
        async def send(self, data):
            sent.append(json.loads(data))

        async def close(self):
            self.closed = True

    async def fake_connect(url):
        assert url == "wss://example.test/events"
        return Socket()

    monkeypatch.setattr(
        "app.infrastructure.navixy_ws.websockets.connect",
        fake_connect,
    )
    client = NavixyWebSocketClient("wss://example.test/events", "hash-1")

    async def connect():
        subscription = await client.connect([4, 5])
        await subscription.close()

    asyncio.run(connect())
    assert sent[0]["requests"][0]["target"]["type"] == "selected"
    assert sent[0]["requests"][0]["target"]["tracker_ids"] == [4, 5]
    assert sent[0]["requests"][1]["target"]["tracker_ids"] == [4, 5]


def test_connect_refuses_empty_tracker_ids(monkeypatch):
    called = False

    async def fake_connect(url):
        nonlocal called
        called = True
        raise AssertionError("should not connect")

    monkeypatch.setattr(
        "app.infrastructure.navixy_ws.websockets.connect",
        fake_connect,
    )
    client = NavixyWebSocketClient("wss://example.test/events", "hash-1")

    with pytest.raises(ValueError, match="tracker_ids is empty"):
        asyncio.run(client.connect([]))
    assert called is False


def test_receive_turns_socket_errors_into_source_errors():
    class BrokenSocket:
        async def recv(self):
            raise ConnectionError("reset")

        async def close(self):
            return None

    subscription = NavixySubscription(BrokenSocket(), {1}, idle_timeout=1)
    with pytest.raises(SourceConnectionError, match="receive failed"):
        asyncio.run(subscription.receive())
