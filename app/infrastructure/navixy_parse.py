"""Pure Navixy payload parsers.

Poll and websocket frames do not use the same keys. One state parser
reads both, with the poll shape winning when both are present:

- coordinates: ``gps.location.lat/lng``, otherwise top-level ``lat/lng``
- altitude: ``gps.alt``, then ``gps.altitude``, then top-level ``alt``,
  then top-level ``altitude``
- time: ``gps.updated``, then ``timestamp``, then ``updated``
- speed and heading: the ``gps`` value, otherwise the top-level value
- ``connection_status`` and ``movement_status`` from the state object

A missing timestamp stays missing. It is not replaced with the clock.
A value with no offset is UTC. Aware values are converted to UTC. The
poll and websocket parsers share this rule. ``received_timestamp`` keeps
the parsed value from before that conversion, including a naive timezone.
"""

from datetime import datetime
from typing import Any

from app.domain.entities import TrackerPosition
from app.domain.errors import TrackerDiscoveryError
from navtracker_common.timeutil import ensure_utc


def parse_poll_response(payload: Any, tracker_id: int) -> TrackerPosition:
    """Parse a ``tracker/get_state`` body for the tracker that was requested."""

    if not isinstance(payload, dict):
        raise RuntimeError(
            f"Navixy poll response for tracker_id={tracker_id} is not an object"
        )
    if payload.get("success") is False:
        raise RuntimeError(
            f"Navixy poll response for tracker_id={tracker_id} was not successful"
        )
    state = payload.get("state")
    if not isinstance(state, dict):
        raise RuntimeError(
            f"Navixy poll response for tracker_id={tracker_id} has no state"
        )
    position = parse_state(state, tracker_id=tracker_id)
    if position is None:
        raise RuntimeError(
            f"Navixy poll response for tracker_id={tracker_id} has no location"
        )
    return position


def parse_websocket_event(event: dict[str, Any]) -> list[TrackerPosition]:
    """Parse one websocket frame into zero or more positions.

    Flat ``location`` events (and frames with no type) are the shape this
    service already receives. ``state_batch`` carries the same state
    objects as the poll API, one per tracker.
    """

    event_type = event.get("type")
    if event_type in (None, "location"):
        position = parse_state(event)
        return [position] if position is not None else []
    if event_type == "state_batch":
        data = event.get("data")
        if not isinstance(data, list):
            return []
        positions: list[TrackerPosition] = []
        for item in data:
            if not isinstance(item, dict):
                continue
            position = parse_state(item)
            if position is not None:
                positions.append(position)
        return positions
    return []


def parse_state(
    state: dict[str, Any],
    *,
    tracker_id: int | None = None,
) -> TrackerPosition | None:
    gps = state.get("gps")
    gps_dict = gps if isinstance(gps, dict) else {}
    location = gps_dict.get("location")
    location_dict = location if isinstance(location, dict) else {}

    latitude = _first_float(location_dict.get("lat"), state.get("lat"))
    longitude = _first_float(location_dict.get("lng"), state.get("lng"))
    if latitude is None or longitude is None:
        return None

    resolved_id = tracker_id
    if resolved_id is None:
        resolved_id = _tracker_id(state.get("tracker_id"), state.get("source_id"))
    if resolved_id is None:
        return None

    timestamp, received = _first_timestamp(
        gps_dict.get("updated"),
        state.get("timestamp"),
        state.get("updated"),
    )
    return TrackerPosition(
        tracker_id=resolved_id,
        latitude=latitude,
        longitude=longitude,
        timestamp=timestamp,
        received_timestamp=received,
        speed=_first_float(gps_dict.get("speed"), state.get("speed")),
        heading=_first_float(gps_dict.get("heading"), state.get("heading")),
        altitude=_first_float(
            gps_dict.get("alt"),
            gps_dict.get("altitude"),
            state.get("alt"),
            state.get("altitude"),
        ),
        connection_status=_text(state.get("connection_status")),
        movement_status=_text(state.get("movement_status")),
    )


def _tracker_id(*values: object) -> int | None:
    for value in values:
        parsed = _as_int(value)
        if parsed is not None:
            return parsed
    return None


def _as_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str) and value.strip():
        try:
            return int(value)
        except ValueError:
            return None
    return None


def _first_float(*values: object) -> float | None:
    for value in values:
        parsed = _as_float(value)
        if parsed is not None:
            return parsed
    return None


def _as_float(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    if isinstance(value, str) and not value.strip():
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _first_timestamp(
    *values: object,
) -> tuple[datetime | None, datetime | None]:
    """Return ``(utc, as_parsed)`` for the first usable timestamp."""

    for value in values:
        parsed = _timestamp(value)
        if parsed is not None:
            return ensure_utc(parsed), parsed
    return None, None


def _timestamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def parse_tracker_list(payload: object) -> list[int]:
    """Parse a ``tracker/list`` body into tracker ids.

    ``success: false``, a missing ``list``, or an entry without an integer
    ``id`` fails the whole response. The message does not include the body.
    An empty ``list`` is a successful read of no trackers.
    """

    if not isinstance(payload, dict):
        raise TrackerDiscoveryError(
            "Navixy tracker list response is not an object"
        )
    if payload.get("success") is False:
        raise TrackerDiscoveryError(
            "Navixy tracker list response was not successful"
        )
    raw = payload.get("list")
    if not isinstance(raw, list):
        raise TrackerDiscoveryError(
            "Navixy tracker list response has no list"
        )
    ids: list[int] = []
    for item in raw:
        if not isinstance(item, dict):
            raise TrackerDiscoveryError(
                "Navixy tracker list entry is not an object"
            )
        tracker_id = item.get("id")
        if isinstance(tracker_id, bool) or not isinstance(tracker_id, int):
            raise TrackerDiscoveryError(
                "Navixy tracker list entry has no integer id"
            )
        ids.append(tracker_id)
    return list(dict.fromkeys(ids))


def _text(value: object) -> str | None:
    if isinstance(value, str) and value.strip():
        return value
    return None
