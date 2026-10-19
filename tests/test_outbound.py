import json
from datetime import datetime, timezone

from app.application.messages import to_outbound_message
from app.domain.entities import TrackerPosition
from app.domain.publication import SCHEMA_VERSION

WHEN = datetime(2026, 1, 1, tzinfo=timezone.utc)


def test_outbound_message_is_versioned_and_not_the_domain_object():
    position = TrackerPosition(
        tracker_id=4,
        latitude=1.5,
        longitude=2.5,
        timestamp=WHEN,
        speed=0.0,
        heading=180.0,
        altitude=12.0,
        connection_status="active",
        movement_status="parked",
    )
    message = to_outbound_message(position)
    payload = message.to_payload()

    assert message.schema_version == SCHEMA_VERSION == 1
    assert payload == {
        "schema_version": 1,
        "tracker_id": 4,
        "latitude": 1.5,
        "longitude": 2.5,
        "timestamp": "2026-01-01T00:00:00+00:00",
        "speed": 0.0,
        "heading": 180.0,
        "altitude": 12.0,
        "connection_status": "active",
        "movement_status": "parked",
    }
    encoded = json.dumps(payload, separators=(",", ":"))
    assert " " not in encoded
    assert encoded.startswith('{"schema_version":1,')
    assert "raw" not in payload
    assert not hasattr(position, "raw")
    assert not hasattr(position, "as_dict")


def test_outbound_message_keeps_nulls_for_unknown_fields():
    payload = to_outbound_message(
        TrackerPosition(tracker_id=1, latitude=1.0, longitude=2.0)
    ).to_payload()
    assert payload["schema_version"] == 1
    assert payload["timestamp"] is None
    assert payload["speed"] is None
    assert payload["heading"] is None
    assert payload["altitude"] is None
    assert payload["connection_status"] is None
    assert payload["movement_status"] is None
