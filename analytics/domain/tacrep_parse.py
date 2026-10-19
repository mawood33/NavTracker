"""Coord-card TACREP messages, with the KIND-seq line as a fallback.

A card message is one record or several records in one chat body.
Fields are split on ``//`` with optional space and a stray apostrophe
immediately before the separator.
A record may continue on following lines. The next record starts when
a line carries a callsign and a serial id in the positions the card
defines. Latitude and longitude win over MGRS. Speed is miles per hour
and is stored as meters per second.
"""

import re
from collections.abc import Sequence
from datetime import UTC, datetime

from analytics.domain.coordcard import CoordCard, CoordType, MgrsGrid
from analytics.domain.correlation import (
    CorrelationPolicy,
    SerialId,
    TacrepMessage,
    parse_serial,
    parse_tacrep_line,
    parse_time,
)
from analytics.domain.geo import haversine_meters
from analytics.domain.timeutil import ensure_utc, valid_position

_SEPARATOR = re.compile(r"\s*'?\s*//\s*")
_NUMBER = re.compile(r"-?\d+(?:\.\d+)?")
DEFAULT_FIELDS = ("callsign", "id", "mgrs", "latlon", "azimuth", "speed", "event")
_MPH_TO_MPS = 1609.344 / 3600
_ATTRIBUTE_ROLES = frozenset(
    {
        "callsign",
        "id",
        "mgrs",
        "latlon",
        "azimuth",
        "speed",
        "event",
        "subject",
        "time",
        "dtg",
        "bmoa",
        "msr",
    }
)


def parse_tacrep_body(
    text: str,
    *,
    source_id: str,
    observed_at: datetime | None = None,
    card: CoordCard | None = None,
    grid: MgrsGrid | None = None,
    policy: CorrelationPolicy | None = None,
) -> tuple[TacrepMessage, ...]:
    """Parse coord-card records, or one KIND-seq line when that is the shape."""

    rules = policy or CorrelationPolicy()
    active = card or CoordCard()
    records = _records(text, active)
    if records is not None:
        moment = ensure_utc(observed_at or datetime.now(UTC))
        found: list[TacrepMessage] = []
        for record in records:
            message = _record_message(
                record,
                source_id=source_id,
                observed_at=moment,
                card=active,
                grid=grid,
            )
            if message is not None:
                found.append(message)
        return tuple(found)
    line = parse_tacrep_line(text, source_id=source_id, policy=rules)
    if line is None:
        return ()
    return (line,)


def _records(text: str, card: CoordCard) -> list[str] | None:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines or not any(_is_coord_line(_parts(line), card) for line in lines):
        return None
    groups: list[list[str]] = []
    for line in lines:
        if _is_coord_line(_parts(line), card) or not groups:
            groups.append([line])
        else:
            groups[-1].append(line)
    return ["\n".join(group) for group in groups]


def _is_coord_line(parts: Sequence[str], card: CoordCard) -> bool:
    if len(parts) < 2:
        return False
    for index, token in enumerate(parts):
        serial = parse_serial(token)
        if serial is None or index != _id_index(card, serial.kind):
            continue
        if index == 0 and len(parts) > 1 and parse_time(parts[1]) is not None:
            return False
        return True
    return False


def _id_index(card: CoordCard, kind: str) -> int:
    for item in card.types:
        if item.kind == kind and "id" in item.fields:
            return item.fields.index("id")
    if "id" in DEFAULT_FIELDS:
        return DEFAULT_FIELDS.index("id")
    return 1


def _parts(line: str) -> list[str]:
    return [part.strip() for part in _SEPARATOR.split(line.strip())]


def _record_message(
    record: str,
    *,
    source_id: str,
    observed_at: datetime,
    card: CoordCard,
    grid: MgrsGrid | None,
) -> TacrepMessage | None:
    values = _values(record)
    serial = _serial_at_id(values, card)
    if serial is None:
        return None
    matched, block_label, block_warning = card.match(serial.kind, serial.serial)
    fields = matched.fields if matched is not None else DEFAULT_FIELDS
    assigned, attributes = _assign(fields, values)
    warnings = [block_warning] if block_warning else []
    latitude, longitude = _position(assigned, card, grid, warnings)
    if latitude is None or longitude is None:
        return None
    timed = parse_time(assigned.get("time", "") or assigned.get("dtg", ""))
    speed = _speed_mps(assigned.get("speed", ""))
    heading = _float_token(assigned.get("azimuth", ""))
    tacrep_type = matched.name if isinstance(matched, CoordType) else ""
    return TacrepMessage(
        kind=serial.kind,
        sequence=f"{serial.serial:03d}{serial.suffix}",
        observed_at=timed or observed_at,
        latitude=latitude,
        longitude=longitude,
        source_id=source_id,
        source_message_id=serial.full_id,
        subject=assigned.get("subject", ""),
        speed_mps=speed,
        heading=heading,
        bmoa=assigned.get("bmoa", ""),
        msr=assigned.get("msr", ""),
        raw=record,
        serial_base=serial.base_id,
        serial_full=serial.full_id,
        correction_rank=serial.rank,
        tacrep_type=tacrep_type,
        block_label=block_label,
        block_warning="; ".join(warnings),
        event=assigned.get("event", ""),
        callsign=assigned.get("callsign", ""),
        mgrs_text=assigned.get("mgrs", ""),
        attributes=tuple(attributes),
    )


def _values(record: str) -> list[str]:
    values: list[str] = []
    lines = [line.strip() for line in record.splitlines() if line.strip()]
    for index, line in enumerate(lines):
        parts = _parts(line)
        if index > 0 and parts and parts[0] == "":
            parts = parts[1:]
        values.extend(parts)
    return values


def _serial_at_id(values: Sequence[str], card: CoordCard) -> SerialId | None:
    for index, token in enumerate(values):
        serial = parse_serial(token)
        if serial is not None and index == _id_index(card, serial.kind):
            return serial
    return None


def _assign(
    fields: Sequence[str],
    values: Sequence[str],
) -> tuple[dict[str, str], list[tuple[str, str]]]:
    assigned: dict[str, str] = {}
    attributes: list[tuple[str, str]] = []
    for index, value in enumerate(values):
        if index < len(fields):
            key = fields[index]
            assigned[key] = value
            if key not in _ATTRIBUTE_ROLES and value:
                attributes.append((key, value))
            continue
        if value:
            attributes.append((f"extra_{index + 1}", value))
    return assigned, attributes


def _position(
    assigned: dict[str, str],
    card: CoordCard,
    grid: MgrsGrid | None,
    warnings: list[str],
) -> tuple[float | None, float | None]:
    point = _latlon(assigned.get("latlon", ""))
    mgrs_point = None
    mgrs_text = assigned.get("mgrs", "").strip()
    if mgrs_text and grid is not None:
        mgrs_point = grid.to_latlon(mgrs_text)
    if point is not None and mgrs_point is not None:
        distance = haversine_meters(point[0], point[1], mgrs_point[0], mgrs_point[1])
        if distance > card.mismatch_meters:
            warnings.append(f"lat/lon and MGRS differ by {distance:.0f} m")
    if point is not None:
        return point
    return mgrs_point if mgrs_point is not None else (None, None)


def _latlon(text: str) -> tuple[float, float] | None:
    numbers = _NUMBER.findall(text)
    if len(numbers) != 2:
        return None
    latitude = float(numbers[0])
    longitude = float(numbers[1])
    if not valid_position(latitude, longitude):
        return None
    return latitude, longitude


def _speed_mps(text: str) -> float | None:
    number = _float_token(text.lower().replace("mph", ""))
    if number is None:
        return None
    return number * _MPH_TO_MPS


def _float_token(text: str) -> float | None:
    match = _NUMBER.search(text)
    if match is None:
        return None
    return float(match.group(0))
