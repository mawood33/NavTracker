from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta


@dataclass(frozen=True)
class GpsFix:
    """One position observation from any source."""

    vehicle_id: str
    latitude: float
    longitude: float
    timestamp: datetime
    speed_mps: float | None = None
    heading: float | None = None
    source: str = ""

    def with_speed(self, speed_mps: float | None) -> "GpsFix":
        return GpsFix(
            vehicle_id=self.vehicle_id,
            latitude=self.latitude,
            longitude=self.longitude,
            timestamp=self.timestamp,
            speed_mps=speed_mps,
            heading=self.heading,
            source=self.source,
        )


def dedupe_fixes(
    fixes: Sequence[GpsFix],
    tolerance_seconds: float = 0,
) -> list[GpsFix]:
    """Keep one fix per vehicle and timestamp.

    The same vehicle and timestamp from two sources collapses to the
    first fix. A later copy can fill in a missing speed. ``tolerance_seconds``
    buckets timestamps so near-duplicates from unsynchronized sources
    also collapse. Zero requires an equal timestamp.
    """

    if tolerance_seconds < 0:
        raise ValueError("dedupe tolerance must be >= 0")
    chosen: dict[tuple[str, datetime], GpsFix] = {}
    order: list[tuple[str, datetime]] = []
    for fix in fixes:
        key = (fix.vehicle_id, _bucket(fix.timestamp, tolerance_seconds))
        current = chosen.get(key)
        if current is None:
            chosen[key] = fix
            order.append(key)
            continue
        if current.speed_mps is None and fix.speed_mps is not None:
            chosen[key] = current.with_speed(fix.speed_mps)
    return [chosen[key] for key in order]


def _bucket(timestamp: datetime, tolerance_seconds: float) -> datetime:
    if tolerance_seconds <= 0:
        return timestamp
    epoch = timestamp.timestamp()
    bucket = int(epoch // tolerance_seconds) * tolerance_seconds
    return datetime.fromtimestamp(bucket, tz=timestamp.tzinfo)


def fix_from_mapping(
    payload: Mapping[str, object],
    *,
    source: str,
    speed_unit: str = "mps",
) -> GpsFix | None:
    """Read a position object.

    Accepts the ChatSurfer outbound document (``tracker_id``, ``speed``)
    and the Clairvoyant stub shape (``vehicle_id``, ``speed_mps``).
    ``speed_unit`` is ``mps`` or ``kmh`` and applies only to ``speed``.
    ``speed_mps`` is always meters per second.
    """

    vehicle = _vehicle_id(payload)
    latitude = _float_field(payload, "latitude", "lat")
    longitude = _float_field(payload, "longitude", "lon", "lng")
    timestamp = _timestamp(payload.get("timestamp"))
    if vehicle is None or latitude is None or longitude is None or timestamp is None:
        return None
    speed = _speed(payload, speed_unit)
    heading = _float_field(payload, "heading")
    return GpsFix(
        vehicle_id=vehicle,
        latitude=latitude,
        longitude=longitude,
        timestamp=timestamp,
        speed_mps=speed,
        heading=heading,
        source=source,
    )


def _vehicle_id(payload: Mapping[str, object]) -> str | None:
    for key in ("vehicle_id", "tracker_id", "id"):
        value = payload.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, int):
            return str(value)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _float_field(payload: Mapping[str, object], *keys: str) -> float | None:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, bool) or value is None:
            continue
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            try:
                return float(value.strip())
            except ValueError:
                return None
    return None


def _speed(payload: Mapping[str, object], speed_unit: str) -> float | None:
    direct = _float_field(payload, "speed_mps")
    if direct is not None:
        return direct
    speed = _float_field(payload, "speed")
    if speed is None:
        return None
    if speed_unit == "kmh":
        return speed / 3.6
    return speed


def _timestamp(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed


def seconds_between(start: datetime, end: datetime) -> float:
    return (end - start).total_seconds()


def add_seconds(moment: datetime, seconds: float) -> datetime:
    return moment + timedelta(seconds=seconds)
