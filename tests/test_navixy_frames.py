import asyncio
import json
import logging
from urllib.parse import parse_qsl, urlsplit

from app.infrastructure.navixy_ws import (
    NavixySubscription,
    inspect_navixy_frame,
    with_atmosphere_tracking,
)

_HANDSHAKE = "b623a15d-9623-4fd8-a9d3-697036635c29|30000|X|"


class _Socket:
    def __init__(self, frames):
        self.frames = list(frames)

    async def recv(self):
        if not self.frames:
            await asyncio.Event().wait()
        return self.frames.pop(0)

    async def close(self):
        return None


def _position(tracker_id: int) -> str:
    return json.dumps(
        {
            "tracker_id": tracker_id,
            "lat": 1.5,
            "lng": 2.5,
            "timestamp": "2026-01-01T00:00:00Z",
        }
    )


def _prefixed(body: str) -> str:
    return f"{len(body)}|{body}"


def _documented_batch(tracker_id: int) -> str:
    body = json.dumps(
        {
            "type": "event",
            "event": "state_batch",
            "data": [
                {
                    "source_id": tracker_id,
                    "gps": {
                        "updated": "2026-01-01T00:00:00Z",
                        "location": {"lat": 4, "lng": 5},
                    },
                }
            ],
        }
    )
    return _prefixed(body)


async def _receive(frames, tracker_ids, count):
    subscription = NavixySubscription(
        _Socket(frames),
        set(tracker_ids),
        idle_timeout=1,
    )
    found = []
    try:
        for _ in range(count):
            found.append(await subscription.receive())
        alive = subscription.last_alive
    finally:
        await subscription.close()
    return found, alive


def test_atmosphere_query_is_added_once_and_length_tracking_stays_off():
    url = with_atmosphere_tracking("wss://ws.navixy.com/events")
    parts = urlsplit(url)
    assert parts.path == "/events"
    assert parts.netloc == "ws.navixy.com"
    pairs = parse_qsl(parts.query)
    keys = [key for key, _value in pairs]
    assert keys.count("X-Atmosphere-tracking-id") == 1
    assert dict(pairs)["X-Atmosphere-tracking-id"] == "0"
    assert "X-Atmosphere-TrackMessageSize" not in keys

    kept = with_atmosphere_tracking(
        "wss://ws.navixy.com/events?X-Atmosphere-tracking-id=0&foo=1"
        "&X-Atmosphere-TrackMessageSize=true"
    )
    kept_pairs = parse_qsl(urlsplit(kept).query)
    assert [key for key, _value in kept_pairs].count(
        "X-Atmosphere-tracking-id"
    ) == 1
    assert ("foo", "1") in kept_pairs
    assert ("X-Atmosphere-TrackMessageSize", "true") in kept_pairs
    assert urlsplit(kept).path == "/events"


def test_heartbeat_frames_mark_the_socket_alive_at_debug(caplog):
    caplog.set_level(logging.DEBUG)
    found, alive = asyncio.run(
        _receive(["X", "X\n", "  ", "", b"X", _position(1)], {1}, 6)
    )
    assert found[:5] == [None, None, None, None, None]
    assert found[5] is not None
    assert found[5].tracker_id == 1
    assert alive > 0
    assert caplog.text.count("Navixy websocket heartbeat") == 5
    assert not [record for record in caplog.records if record.levelno >= logging.WARNING]


def test_handshake_frame_is_debug_and_keeps_the_socket_alive(caplog):
    caplog.set_level(logging.DEBUG)
    found, alive = asyncio.run(
        _receive([_HANDSHAKE, _HANDSHAKE + "\n", _position(4)], {4}, 3)
    )
    assert found[0] is None
    assert found[1] is None
    assert found[2] is not None
    assert found[2].tracker_id == 4
    assert alive > 0
    assert caplog.text.count("Navixy websocket Atmosphere handshake") == 2
    assert "Ignoring unparseable" not in caplog.text
    assert not [record for record in caplog.records if record.levelno >= logging.WARNING]


def test_length_prefixed_state_batch_is_a_current_position():
    found, alive = asyncio.run(_receive([_documented_batch(3)], {3}, 1))
    assert found[0] is not None
    assert found[0].tracker_id == 3
    assert found[0].latitude == 4
    assert alive > 0

    single = json.dumps(
        {
            "type": "event",
            "event": "state",
            "data": {
                "source_id": 8,
                "gps": {"location": {"lat": 6, "lng": 7}},
            },
        }
    )
    found, _alive = asyncio.run(_receive([_prefixed(single)], {8}, 1))
    assert found[0] is not None
    assert found[0].tracker_id == 8


def test_one_frame_can_carry_several_messages_and_a_heartbeat():
    first = _position(1)
    second = _position(3)
    combined = f"{len(first)}|{first}{len(second)}|{second}"
    padded = f"1|X{_documented_batch(9)}"
    found, alive = asyncio.run(_receive([combined, padded], {1, 3, 9}, 3))
    assert [item.tracker_id for item in found if item is not None] == [1, 3, 9]
    assert alive > 0

    frame = inspect_navixy_frame(padded)
    assert frame.heartbeat is True
    assert len(frame.events) == 1
    assert frame.unparseable == ()


def test_truncated_and_unknown_frames_warn_with_a_short_preview(caplog):
    caplog.set_level(logging.WARNING)
    tail = "A" * 80 + "SECRET-TAIL"
    subscription = NavixySubscription(
        _Socket(['7|{"a":', tail, _position(1)]),
        {1},
        idle_timeout=1,
    )

    async def run():
        first = await subscription.receive()
        after_truncated = subscription.last_alive
        second = await subscription.receive()
        after_unknown = subscription.last_alive
        third = await subscription.receive()
        await subscription.close()
        return first, after_truncated, second, after_unknown, third

    first, after_truncated, second, after_unknown, third = asyncio.run(run())
    assert first is None
    assert second is None
    assert after_truncated == 0
    assert after_unknown == 0
    assert third is not None
    assert third.tracker_id == 1
    assert subscription.last_alive > 0
    warnings = [
        record.getMessage()
        for record in caplog.records
        if record.levelno >= logging.WARNING
    ]
    assert len(warnings) == 2
    assert all(
        message.startswith("Ignoring unparseable Navixy websocket frame")
        for message in warnings
    )
    assert "SECRET-TAIL" not in caplog.text
    assert "..." in warnings[1]
