import asyncio
import json
import logging
from urllib.parse import parse_qsl, urlsplit

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
    assert message["iso_datetime"] is True
    assert "trackers" not in message
    assert message["requests"][0]["target"] == {
        "type": "selected",
        "tracker_ids": [9, 8],
    }
    assert message["requests"][0]["rate_limit"] == "5s"
    assert message["requests"][0]["format"] == "full"
    assert message["requests"][1]["type"] == "readings_batch"
    assert message["requests"][1]["rate_limit"] == "5s"
    assert "format" not in message["requests"][1]
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


def test_state_batch_is_parsed_like_poll_and_filtered_to_scope():
    state = {
        "source_id": 3,
        "gps": {
            "updated": "2026-01-01T00:00:00Z",
            "location": {"lat": 4, "lng": 5},
            "alt": 6,
        },
        "connection_status": "active",
        "movement_status": "moving",
    }
    frame = json.dumps(
        {
            "type": "state_batch",
            "data": [
                {"source_id": 99, "gps": {"location": {"lat": 1, "lng": 1}}},
                state,
                {"tracker_id": 1, "lat": 8, "lng": 9},
            ],
        }
    )
    socket = _Socket([frame])
    subscription = NavixySubscription(socket, {1, 3}, idle_timeout=1)

    async def collect():
        found = []
        for _ in range(2):
            found.append(await subscription.receive())
        await subscription.close()
        return found

    found = asyncio.run(collect())
    assert [item.tracker_id for item in found] == [3, 1]
    assert found[0].altitude == 6.0
    assert found[0].connection_status == "active"
    assert found[0].movement_status == "moving"
    assert found[1].timestamp is None


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

        async def recv(self):
            return json.dumps(
                {
                    "type": "response",
                    "action": "subscription/subscribe",
                    "events": ["state_batch", "readings_batch"],
                    "data": {
                        "state_batch": {
                            "success": True,
                            "value": {"4": "normal", "5": "normal"},
                        }
                    },
                }
            )

        async def close(self):
            self.closed = True

    seen = []

    async def fake_connect(url):
        seen.append(url)
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
    parts = urlsplit(seen[0])
    assert parts.scheme == "wss"
    assert parts.netloc == "example.test"
    assert parts.path == "/events"
    query = dict(parse_qsl(parts.query))
    assert query["X-Atmosphere-tracking-id"] == "0"
    assert query["X-Atmosphere-Framework"] == "2.3.6-javascript"
    assert query["X-Atmosphere-Transport"] == "websocket"
    assert query["Content-Type"] == "application/json"
    assert query["X-atmo-protocol"] == "true"
    assert query["iso_datetime"] == "true"
    assert "X-Atmosphere-TrackMessageSize" not in query
    assert "hash-1" not in seen[0]
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


def test_receive_logs_socket_exception_with_context(caplog):
    caplog.set_level(logging.ERROR)

    class BrokenSocket:
        async def recv(self):
            raise ConnectionError("reset by peer")

        async def close(self):
            return None

    subscription = NavixySubscription(
        BrokenSocket(),
        {1, 3},
        idle_timeout=1,
        ws_url="wss://ws.example.test/events",
    )
    with pytest.raises(SourceConnectionError, match="receive failed"):
        asyncio.run(subscription.receive())

    text = caplog.text
    assert "Navixy websocket receive failed" in text
    assert "url=wss://ws.example.test/events" in text
    assert "tracker_ids=[1, 3]" in text
    assert "reset by peer" in text
    assert "ConnectionError" in text


def test_connect_logs_socket_exception_with_context(caplog, monkeypatch):
    caplog.set_level(logging.ERROR)

    async def fake_connect(url):
        raise ConnectionError("connection refused")

    monkeypatch.setattr(
        "app.infrastructure.navixy_ws.websockets.connect",
        fake_connect,
    )
    client = NavixyWebSocketClient("wss://ws.example.test/events", "hash-1")
    with pytest.raises(SourceConnectionError, match="connection failed"):
        asyncio.run(client.connect([8, 2]))

    text = caplog.text
    assert "Navixy websocket connection failed" in text
    assert "url=wss://ws.example.test/events" in text
    assert "tracker_ids=[2, 8]" in text
    assert "connection refused" in text
    assert "hash-1" not in text
