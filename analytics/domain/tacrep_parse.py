"""Coord-card TACREP messages, with the KIND-seq line as a fallback.

A card message is one record or several records in one chat body.
Fields are split on ``//`` with optional space and a stray apostrophe
immediately before the separator.
A record may continue on following lines. The next record starts when
a line carries a serial id in the position the card defines.

Field names are roles. The shipped exercise formats use ``activity``,
``bullseye``, ``location``, ``tai``, ``tai_msr``, ``cat``, ``ellipse``,
``orientation``, ``fillins``, and ``correlates``. Unknown names stay
attributes, so another exercise can add fields without a code change.
``UPDATE <id>`` uses the FUSED field order and supersedes that base id.
"""

import re
from collections.abc import Sequence
from datetime import UTC, datetime

from analytics.domain.confidence_text import split_reported_confidence
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
from analytics.domain.positions import (
    missing_text,
    parse_cat,
    parse_correlates,
    parse_ellipse,
    parse_fillins,
    parse_location,
    parse_orientation,
    parse_report_time,
    split_location_tai,
    split_tai_msr,
    strip_invisible,
)
from analytics.domain.templates import Tasking, parse_templated
from analytics.domain.timeutil import ensure_utc

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
        "location",
        "azimuth",
        "speed",
        "event",
        "subject",
        "time",
        "dtg",
        "bmoa",
        "msr",
        "tai",
        "tai_msr",
    }
)
_ACTIVITY_EVENTS = {
    "milling": "milling",
    "mill": "milling",
    "rotator": "rotator",
    "mover": "mover",
    "movement": "mover",
    "halt": "halt",
    "stopping": "stopping",
    "stop": "stop",
    "slowing": "slowing",
    "slowdown": "slowdown",
}


def parse_exercise_text(
    text: str,
    *,
    source_id: str,
    observed_at: datetime | None = None,
    card: CoordCard | None = None,
    grid: MgrsGrid | None = None,
    policy: CorrelationPolicy | None = None,
) -> tuple[tuple[TacrepMessage, ...], tuple[Tasking, ...]]:
    """Parse a template body, or TACREP records when no template matches."""

    active = card or CoordCard()
    cleaned = strip_invisible(text)
    templated = parse_templated(
        cleaned,
        active,
        source_id=source_id,
        observed_at=observed_at,
        grid=grid,
    )
    if templated is not None:
        return templated.messages, templated.taskings
    return (
        parse_tacrep_body(
            cleaned,
            source_id=source_id,
            observed_at=observed_at,
            card=active,
            grid=grid,
            policy=policy,
        ),
        (),
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
    text = strip_invisible(text)
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
        if serial is None or index != _id_index(card, serial.kind, update=serial.updated):
            continue
        if index == 0 and len(parts) > 1 and parse_time(parts[1]) is not None:
            return False
        return True
    return False


def _id_index(card: CoordCard, kind: str, *, update: bool = False) -> int:
    if update:
        fused = card.fused_type()
        if fused is not None and "id" in fused.fields:
            return fused.fields.index("id")
    for item in card.types:
        if kind in item.kinds and "id" in item.fields:
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
    fields = _layout(card, serial, matched)
    assigned, attributes = _assign(fields, values)
    warnings = [block_warning] if block_warning else []
    _apply_location_tai(assigned, warnings)
    latitude, longitude, mgrs_text = _position(assigned, card, grid, warnings)
    if latitude is None or longitude is None:
        return None
    timed = parse_report_time(
        assigned.get("time", "") or assigned.get("dtg", ""),
        observed_at,
    )
    speed = _speed_mps(assigned.get("speed", ""))
    heading = _float_token(assigned.get("azimuth", ""))
    vehicles, fill_speed, fill_heading, halted = parse_fillins(assigned.get("fillins", ""))
    if speed is None:
        speed = fill_speed
    if heading is None:
        heading = fill_heading
    events = _events(assigned.get("activity", ""), assigned.get("event", ""), halted)
    area, route = _areas(assigned)
    ellipse = parse_ellipse(assigned.get("ellipse", ""), card.ellipse_unit)
    orientation = parse_orientation(
        assigned.get("orientation", ""),
        card.magnetic_declination_degrees,
    )
    level = parse_cat(assigned.get("cat", ""))
    phrase, level_name, band, weight, attributes = _confidence(assigned, attributes, card)
    tacrep_type = matched.name if isinstance(matched, CoordType) else ""
    subject = assigned.get("subject", "")
    equipment = _equipment(assigned.get("equipment", ""))
    vacant = _vacant(assigned)
    return TacrepMessage(
        kind=serial.kind,
        sequence=f"{serial.serial:03d}{serial.suffix}",
        observed_at=timed or observed_at,
        latitude=latitude,
        longitude=longitude,
        source_id=source_id,
        source_message_id=serial.full_id,
        subject=subject,
        speed_mps=speed,
        heading=heading,
        bmoa=area,
        msr=route,
        raw=record,
        serial_base=serial.base_id,
        serial_full=serial.full_id,
        correction_rank=serial.rank,
        tacrep_type=tacrep_type,
        block_label=block_label,
        block_warning="; ".join(warning for warning in warnings if warning),
        event=events[0] if events else "",
        callsign=assigned.get("callsign", ""),
        mgrs_text=mgrs_text,
        attributes=tuple(attributes),
        semi_major_m=None if ellipse is None else ellipse[0],
        semi_minor_m=None if ellipse is None else ellipse[1],
        orientation_deg=orientation,
        cat=assigned.get("cat", ""),
        cat_level=level,
        ce_radius_m=None if level is None else card.ce_radius_m(level),
        reported_confidence=weight,
        confidence_phrase=phrase,
        confidence_level=level_name,
        confidence_band=band,
        vehicle_count=vehicles,
        equipment=equipment,
        vacant=vacant,
        correlates_to=parse_correlates(assigned.get("correlates", "")),
        events=events,
        bullseye=assigned.get("bullseye", ""),
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
        if serial is not None and index == _id_index(card, serial.kind, update=serial.updated):
            return serial
    return None


def _layout(card: CoordCard, serial: SerialId, matched: CoordType | None) -> tuple[str, ...]:
    if serial.updated:
        fused = card.fused_type()
        if fused is not None:
            return fused.fields
        return ("callsign", "id", "subject", "location", "cat", "time", "correlates")
    if matched is not None:
        return matched.fields
    return DEFAULT_FIELDS


def _assign(
    fields: Sequence[str],
    values: Sequence[str],
) -> tuple[dict[str, str], list[tuple[str, str]]]:
    assigned: dict[str, str] = {}
    attributes: list[tuple[str, str]] = []
    for index, value in enumerate(values):
        token = strip_invisible(value).strip()
        if missing_text(token):
            token = ""
        if index < len(fields):
            key = fields[index]
            assigned[key] = token
            if key not in _ATTRIBUTE_ROLES and token:
                attributes.append((key, token))
            continue
        if token:
            attributes.append((f"extra_{index + 1}", token))
    return assigned, attributes


def _apply_location_tai(assigned: dict[str, str], warnings: list[str]) -> None:
    location = assigned.get("location", "")
    if not location:
        return
    body, tail = split_location_tai(location)
    assigned["location"] = body
    if not tail:
        return
    current = assigned.get("tai", "")
    if not current:
        assigned["tai"] = tail
        return
    if current.strip().lower() != tail.strip().lower():
        warnings.append(f"location TAI {tail} differs from TAI field {current}")


def _areas(assigned: dict[str, str]) -> tuple[str, str]:
    area = assigned.get("bmoa", "") or assigned.get("tai", "")
    route = assigned.get("msr", "")
    combined = assigned.get("tai_msr", "")
    if combined:
        tai, msr = split_tai_msr(combined)
        area = area or tai
        route = route or msr
    return area, route


def _events(activity: str, event: str, halted: bool) -> tuple[str, ...]:
    found: list[str] = []
    for token in (activity, event):
        for part in re.split(r"[,/]|\band\b", token):
            mapped = _ACTIVITY_EVENTS.get(part.strip().lower())
            if mapped and mapped not in found:
                found.append(mapped)
    if halted and "halt" not in found:
        found.append("halt")
    return tuple(found)


def _position(
    assigned: dict[str, str],
    card: CoordCard,
    grid: MgrsGrid | None,
    warnings: list[str],
) -> tuple[float | None, float | None, str]:
    location = assigned.get("location", "").strip()
    point = parse_location(assigned.get("latlon", "") or location, grid)
    mgrs_point = None
    mgrs_text = assigned.get("mgrs", "").strip()
    if not mgrs_text and location and grid is not None and grid.to_latlon(location) is not None:
        mgrs_text = location
    if mgrs_text and grid is not None:
        mgrs_point = grid.to_latlon(mgrs_text)
    if point is not None and mgrs_point is not None:
        distance = haversine_meters(point[0], point[1], mgrs_point[0], mgrs_point[1])
        if distance > card.mismatch_meters:
            warnings.append(f"lat/lon and MGRS differ by {distance:.0f} m")
    if point is not None:
        return point[0], point[1], mgrs_text
    if mgrs_point is not None:
        return mgrs_point[0], mgrs_point[1], mgrs_text
    return None, None, mgrs_text


def _confidence(
    assigned: dict[str, str],
    attributes: list[tuple[str, str]],
    card: CoordCard,
) -> tuple[str, str, str, float | None, list[tuple[str, str]]]:
    phrase = ""
    level_name = ""
    band = ""
    weight: float | None = None
    for key in ("subject", "equipment", "entity", "activity", "callsign"):
        raw = assigned.get(key, "")
        if not raw:
            continue
        term, rest = split_reported_confidence(raw, card.confidence_terms)
        if term is None:
            continue
        assigned[key] = rest
        attributes = [
            (name, rest if name == key and value == raw else value)
            for name, value in attributes
        ]
        if not phrase:
            phrase = term.phrase
            level_name = term.level
            band = term.band
            weight = term.weight
    attributes = [(name, value) for name, value in attributes if value and not missing_text(value)]
    return phrase, level_name, band, weight, attributes


def _equipment(text: str) -> tuple[str, ...]:
    if missing_text(text):
        return ()
    return tuple(part.strip() for part in text.split(",") if part.strip() and part.strip() != "-")


def _vacant(assigned: dict[str, str]) -> bool:
    for key in ("activity", "fillins", "tai", "subject", "entity"):
        if re.search(r"\bVACANT\b", assigned.get(key, ""), re.IGNORECASE):
            return True
    return False


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
