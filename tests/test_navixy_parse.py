from datetime import UTC, datetime, timedelta

import pytest
from app.domain.entities import TrackerPosition
from app.infrastructure.navixy_parse import (
    parse_poll_response,
    parse_state,
    parse_websocket_event,
)

WHEN = datetime(2026, 1, 1, tzinfo=UTC)

STATE = {
    "source_id": 7,
    "gps": {
        "updated": "2026-01-01T00:00:00Z",
        "location": {"lat": 10.5, "lng": -20.25},
        "heading": 90,
        "speed": 0,
        "alt": 12.5,
    },
    "connection_status": "active",
    "movement_status": "parked",
}


def test_poll_and_websocket_parse_the_same_state():
    polled = parse_poll_response({"success": True, "state": STATE}, 7)
    streamed = parse_websocket_event({"type": "state_batch", "data": [dict(STATE)]})
    assert streamed == [polled]
    assert polled == TrackerPosition(
        tracker_id=7,
        latitude=10.5,
        longitude=-20.25,
        timestamp=WHEN,
        speed=0.0,
        heading=90.0,
        altitude=12.5,
        connection_status="active",
        movement_status="parked",
        received_timestamp=WHEN,
    )


def test_poll_uses_the_requested_tracker_id():
    position = parse_poll_response({"success": True, "state": STATE}, 4)
    assert position.tracker_id == 4


def test_poll_rejects_unusable_bodies_without_the_payload():
    secret = "do-not-log-this"
    cases = [
        {"success": False, "secret": secret, "state": STATE},
        {"success": True, "secret": secret},
        {"success": True, "secret": secret, "state": {"source_id": 1}},
        ["not", "an", "object"],
    ]
    for payload in cases:
        with pytest.raises(RuntimeError, match="tracker_id=7") as caught:
            parse_poll_response(payload, 7)
        assert secret not in str(caught.value)


def test_poll_keeps_a_fix_when_the_timestamp_cannot_be_parsed():
    state = {
        "source_id": 1,
        "gps": {
            "updated": "not-a-time",
            "location": {"lat": 1, "lng": 2},
        },
    }
    position = parse_poll_response({"state": state}, 1)
    assert position.timestamp is None
    assert position.latitude == 1.0


def test_websocket_flat_event_keeps_status_and_altitude():
    parsed = parse_websocket_event(
        {
            "type": "location",
            "tracker_id": "9",
            "lat": "1.5",
            "lng": "2.5",
            "altitude": 3,
            "alt": 4,
            "timestamp": "2026-01-01T00:00:00Z",
            "speed": 4,
            "heading": 5,
            "connection_status": "idle",
            "movement_status": "moving",
        }
    )
    assert parsed == [
        TrackerPosition(
            tracker_id=9,
            latitude=1.5,
            longitude=2.5,
            timestamp=WHEN,
            speed=4.0,
            heading=5.0,
            altitude=4.0,
            connection_status="idle",
            movement_status="moving",
            received_timestamp=WHEN,
        )
    ]


def test_nested_gps_wins_over_flat_coordinates():
    position = parse_state(
        {
            "tracker_id": 1,
            "lat": 1,
            "lng": 2,
            "altitude": 9,
            "speed": 3,
            "gps": {
                "alt": 4,
                "speed": 0,
                "location": {"lat": 8, "lng": 9},
            },
        }
    )
    assert position is not None
    assert position.latitude == 8.0
    assert position.longitude == 9.0
    assert position.altitude == 4.0
    assert position.speed == 0.0


def test_space_separated_navixy_timestamp_parses():
    position = parse_state(
        {
            "tracker_id": 1,
            "lat": 1,
            "lng": 2,
            "gps": {"updated": "2026-01-01 00:00:00"},
        }
    )
    assert position is not None
    assert position.timestamp == WHEN
    assert position.timestamp.tzinfo is UTC
    assert position.received_timestamp == datetime(2026, 1, 1)
    assert position.received_timestamp.tzinfo is None


@pytest.mark.parametrize(
    "text",
    [
        "2026-01-01T00:00:00",
        "2026-01-01 00:00:00",
        "2026-01-01T00:00:00Z",
        "2026-01-01T00:00:00+00:00",
        "2026-01-01T02:00:00+02:00",
    ],
)
def test_poll_and_websocket_timestamps_are_utc(text: str) -> None:
    state = {"tracker_id": 7, "lat": 1, "lng": 2, "timestamp": text}
    polled = parse_poll_response({"success": True, "state": state}, 7)
    streamed = parse_websocket_event({"type": "location", **state})
    batch = parse_websocket_event({"type": "state_batch", "data": [state]})
    assert polled.timestamp == WHEN
    assert polled.timestamp is not None
    assert polled.timestamp.tzinfo is UTC
    assert streamed[0].timestamp == polled.timestamp
    assert batch[0].timestamp == polled.timestamp
    assert polled.received_timestamp is not None
    if text.endswith(("Z", "+00:00")):
        assert polled.received_timestamp.tzinfo is not None
        assert polled.received_timestamp.utcoffset() == timedelta(0)
    elif text.endswith("+02:00"):
        assert polled.received_timestamp.utcoffset() == timedelta(hours=2)
    else:
        assert polled.received_timestamp.tzinfo is None
    assert streamed[0].received_timestamp == polled.received_timestamp
    assert batch[0].received_timestamp == polled.received_timestamp


def test_invalid_timestamp_falls_through_to_the_next_field():
    position = parse_state(
        {
            "tracker_id": 1,
            "lat": 1,
            "lng": 2,
            "gps": {"updated": "not-a-time"},
            "timestamp": "2026-02-02T03:04:05+00:00",
        }
    )
    assert position is not None
    assert position.timestamp == datetime(
        2026, 2, 2, 3, 4, 5, tzinfo=UTC
    )


def test_documented_event_envelope_is_current_state():
    batch = parse_websocket_event(
        {"type": "event", "event": "state_batch", "data": [dict(STATE)]}
    )
    single = parse_websocket_event(
        {"type": "event", "event": "state", "data": dict(STATE)}
    )
    assert [item.tracker_id for item in batch] == [7]
    assert [item.tracker_id for item in single] == [7]
    assert batch[0].latitude == 10.5
    assert parse_websocket_event(
        {"type": "event", "event": "readings_batch", "data": [STATE]}
    ) == []
    assert parse_websocket_event(
        {"type": "event", "event": "lifecycle", "data": {}}
    ) == []
    assert parse_websocket_event(
        {"type": "event", "event": "logout", "data": {}}
    ) == []


def test_websocket_ignores_frames_without_a_fix():
    assert parse_websocket_event({"type": "readings_batch", "data": [STATE]}) == []
    assert parse_websocket_event({"type": "location", "tracker_id": 1}) == []
    assert parse_websocket_event({"lat": 1, "lng": 2}) == []
    assert parse_websocket_event(
        {"tracker_id": "nope", "lat": 1, "lng": 2}
    ) == []
    assert parse_websocket_event({"tracker_id": 1, "lat": True, "lng": 2}) == []
    assert parse_websocket_event({"type": "state_batch", "data": {}}) == []
    assert parse_websocket_event(
        {"type": "state_batch", "data": ["skip", {"source_id": 1}]}
    ) == []
    assert parse_websocket_event({}) == []


def test_state_batch_skips_items_without_a_location_and_keeps_the_rest():
    parsed = parse_websocket_event(
        {
            "type": "state_batch",
            "data": [
                {"source_id": 1},
                STATE,
                {"tracker_id": 8, "lat": 3, "lng": 4, "timestamp": ""},
            ],
        }
    )
    assert [item.tracker_id for item in parsed] == [7, 8]
    assert parsed[1].timestamp is None
