"""TACREP locations, military clocks, ellipses, and fill-in text.

Locations accept decimal degrees, MGRS, and ``DDMM.MM`` with a hemisphere
(``N6919 W11919`` is 69°19' N, 119°19' W). A blank field or ``-`` is
missing. An uncertainty ellipse is a semi-major and semi-minor length
plus a magnetic orientation. Magnetic declination is added to that
orientation; zero means magnetic and true north agree. A positive
declination means magnetic north is east of true north, so true azimuth
is magnetic plus declination. A missing orientation keeps the ellipse
and treats the semi-major axis as a circle.
"""

import math
import re
from datetime import UTC, datetime, timedelta

from analytics.domain.coordcard import MgrsGrid
from analytics.domain.geo import EARTH_RADIUS_METERS
from analytics.domain.timeutil import ensure_utc, valid_position

NAUTICAL_MILE_METERS = 1852.0
_HEMISPHERE = re.compile(
    r"([NSns])\s*(\d{3,}(?:\.\d+)?)\s*[,/\s]\s*([EWew])\s*(\d{3,}(?:\.\d+)?)"
)
_NUMBER = re.compile(r"[+-]?\d+(?:\.\d+)?")
_CLOCK = re.compile(
    r"(?:TOI|TIME(?:\s+OF\s+(?:INTERCEPT|IMAGE))?|DTG)?\s*"
    r"(\d{2})(\d{2})(?:(\d{2}))?\s*Z",
    re.IGNORECASE,
)
_VEHICLES = re.compile(
    r"(?:(\d+)\s*(?:x\s*)?(?:veh(?:icle)?s?)\b|\bveh(?:icle)?s?\s*[:=]?\s*(\d+))",
    re.IGNORECASE,
)
_SPEED = re.compile(
    r"(\d+(?:\.\d+)?)\s*(mph|kts|knots|km/h|kmh|m/s)\b",
    re.IGNORECASE,
)
_HEADING = re.compile(
    r"(?:hdg|heading|azimuth)\s*[:=]?\s*(\d+(?:\.\d+)?)",
    re.IGNORECASE,
)
_HALT = re.compile(r"\bhalt\b", re.IGNORECASE)
_CORR = re.compile(r"CORRELATES\s+TO\s+(.+)$", re.IGNORECASE)
_SERIAL = re.compile(r"[A-Z]-\d{3}[A-Z]?")
_TAI_MSR_SPLIT = re.compile(r"\s*(?:/|,|\band\b)\s*", re.IGNORECASE)


_INVISIBLE = re.compile(r"[\u200b\u200c\u200d\u2060\ufeff]")


def strip_invisible(text: str) -> str:
    """Remove zero-width characters, including U+200B from pasted text."""

    return _INVISIBLE.sub("", text)


def missing_text(text: str) -> bool:
    """True for a blank field or a lone dash, the usual empty TACREP mark."""

    token = text.strip()
    return token == "" or token == "-"


def parse_location(
    text: str,
    grid: MgrsGrid | None = None,
) -> tuple[float, float] | None:
    """Read one location token as decimal degrees, hemisphere, or MGRS."""

    body = text.strip()
    if missing_text(body):
        return None
    hemisphere = _hemisphere(body)
    if hemisphere is not None:
        return hemisphere
    decimal = _decimal_pair(body)
    if decimal is not None:
        return decimal
    if grid is None:
        return None
    return grid.to_latlon(body)


def split_location_tai(text: str) -> tuple[str, str]:
    """Split ``N6919 W11919, NORTH`` into a location and a trailing TAI.

    A comma that is part of a decimal pair stays with the location.
    """

    body = text.strip()
    if missing_text(body):
        return "", ""
    if "," not in body:
        return body, ""
    left, right = body.split(",", 1)
    tail = right.strip()
    if not tail or _looks_like_coordinate(tail) or _decimal_pair(body) is not None:
        return body, ""
    if _hemisphere(left) is None and _decimal_pair(left) is None:
        return body, ""
    return left.strip(), tail


def split_tai_msr(text: str) -> tuple[str, str]:
    """Return the named area and the MSR from one combined field."""

    tai = ""
    msr = ""
    for part in _TAI_MSR_SPLIT.split(text.strip()):
        token = part.strip()
        if not token:
            continue
        if re.match(r"MSR\b", token, re.IGNORECASE):
            msr = re.sub(r"^MSR\s*", "", token, flags=re.IGNORECASE).strip() or token
        elif re.match(r"(?:TAI|BMOA)\b", token, re.IGNORECASE):
            tai = re.sub(r"^(?:TAI|BMOA)\s*", "", token, flags=re.IGNORECASE).strip() or token
        elif not tai:
            tai = token
        elif not msr:
            msr = token
    return tai, msr


def parse_ellipse(text: str, unit: str) -> tuple[float, float] | None:
    """Return semi-major and semi-minor in meters.

    Two numbers are required. ``km``, ``m``, or ``nmi`` written on the
    field wins. Otherwise ``unit`` from the coord card is used. The
    default unit is nautical miles. The result is meters.
    """

    if missing_text(text):
        return None
    numbers = _NUMBER.findall(text.replace(",", " "))
    if len(numbers) < 2:
        return None
    major = float(numbers[0])
    minor = float(numbers[1])
    if major <= 0 or minor <= 0:
        return None
    return major * _unit_meters(text, unit), minor * _unit_meters(text, unit)


def parse_orientation(text: str, declination_degrees: float) -> float | None:
    """Return the true orientation in degrees, after magnetic declination."""

    if missing_text(text):
        return None
    match = _NUMBER.search(text)
    if match is None:
        return None
    true = float(match.group(0)) + declination_degrees
    return true % 360


def point_within_ellipse(
    latitude: float,
    longitude: float,
    center_latitude: float,
    center_longitude: float,
    semi_major_m: float,
    semi_minor_m: float,
    orientation_deg: float,
) -> bool:
    """True when the point lies inside the ellipse, including the boundary.

    ``orientation_deg`` is the true azimuth of the semi-major axis,
    clockwise from north.
    """

    if semi_major_m <= 0 or semi_minor_m <= 0:
        return False
    east, north = _local_meters(latitude, longitude, center_latitude, center_longitude)
    theta = math.radians(orientation_deg)
    along = east * math.sin(theta) + north * math.cos(theta)
    cross = east * math.cos(theta) - north * math.sin(theta)
    return (along / semi_major_m) ** 2 + (cross / semi_minor_m) ** 2 <= 1.0


def point_within_radius(
    latitude: float,
    longitude: float,
    center_latitude: float,
    center_longitude: float,
    radius_m: float,
) -> bool:
    """True when the point lies inside a circle, including the boundary."""

    if radius_m <= 0:
        return False
    east, north = _local_meters(latitude, longitude, center_latitude, center_longitude)
    return east * east + north * north <= radius_m * radius_m


def parse_report_time(text: str, observed_at: datetime) -> datetime | None:
    """Resolve ``1919Z`` or ``DDHHMMZ`` against the message date.

    ``1919Z`` is HHMM Zulu. Six digits are day, hour, and minute. A clock
    more than 12 hours away from ``observed_at`` rolls across midnight.
    ISO-8601 is accepted as well.
    """

    moment = ensure_utc(observed_at)
    token = text.strip()
    if missing_text(token):
        return None
    match = _CLOCK.search(token)
    if match is not None and not _iso_date(token):
        hour = int(match.group(2) if match.group(3) else match.group(1))
        minute = int(match.group(3) if match.group(3) else match.group(2))
        day = int(match.group(1)) if match.group(3) else None
        if match.group(3) is None and hour > 23:
            return None
        if minute > 59 or hour > 23:
            return None
        if day is not None:
            return _on_day(moment, day, hour, minute)
        return _near_clock(moment, hour, minute)
    from analytics.domain.correlation import parse_time

    return parse_time(token)


def parse_cat(text: str) -> int | None:
    """Read ``CAT IV``, ``IV``, or ``4`` as a category level."""

    if missing_text(text):
        return None
    body = text.strip().upper()
    body = re.sub(r"^CAT(?:EGORY)?\s*", "", body).strip()
    if not body or body == "-":
        return None
    roman = {"I": 1, "II": 2, "III": 3, "IV": 4, "V": 5, "VI": 6}
    if body in roman:
        return roman[body]
    if body.isdigit():
        level = int(body)
        return level if level > 0 else None
    return None


def parse_fillins(text: str) -> tuple[int | None, float | None, float | None, bool]:
    """Return vehicle count, speed in m/s, heading degrees, and HALT.

    A speed without a unit is ignored. ``mph`` is miles per hour.
    """

    if missing_text(text):
        return None, None, None, False
    vehicles = _VEHICLES.search(text)
    count = None
    if vehicles is not None:
        count = int(vehicles.group(1) or vehicles.group(2))
    speed = None
    speed_match = _SPEED.search(text)
    if speed_match is not None:
        speed = float(speed_match.group(1)) * _speed_scale(speed_match.group(2))
    heading = None
    heading_match = _HEADING.search(text)
    if heading_match is not None:
        heading = float(heading_match.group(1)) % 360
    return count, speed, heading, _HALT.search(text) is not None


def parse_correlates(text: str) -> tuple[str, ...]:
    """Read ``CORRELATES TO J-401, G-501`` as base serials."""

    if missing_text(text):
        return ()
    match = _CORR.search(text.strip())
    body = match.group(1) if match is not None else text
    found: list[str] = []
    for token in _SERIAL.findall(body.upper()):
        if token not in found:
            found.append(token)
    return tuple(found)


def _hemisphere(text: str) -> tuple[float, float] | None:
    match = _HEMISPHERE.search(text)
    if match is None:
        return None
    lat_hem, lat_token, lon_hem, lon_token = match.groups()
    latitude = _dm(lat_token, 90)
    longitude = _dm(lon_token, 180)
    if latitude is None or longitude is None:
        return None
    if lat_hem.upper() == "S":
        latitude = -latitude
    if lon_hem.upper() == "W":
        longitude = -longitude
    if not valid_position(latitude, longitude):
        return None
    return latitude, longitude


def _dm(token: str, max_degrees: int) -> float | None:
    if "." in token:
        whole, frac = token.split(".", 1)
        if not frac.isdigit():
            return None
        fraction = float(f"0.{frac}")
    else:
        whole, fraction = token, 0.0
    if len(whole) < 3 or not whole.isdigit():
        return None
    minutes = int(whole[-2:]) + fraction
    degrees = int(whole[:-2])
    if degrees > max_degrees or minutes >= 60:
        return None
    return degrees + minutes / 60.0


def _decimal_pair(text: str) -> tuple[float, float] | None:
    if re.search(r"[A-Za-z]", text):
        return None
    numbers = _NUMBER.findall(text)
    if len(numbers) != 2:
        return None
    latitude = float(numbers[0])
    longitude = float(numbers[1])
    if not valid_position(latitude, longitude):
        return None
    return latitude, longitude


def _looks_like_coordinate(text: str) -> bool:
    return _hemisphere(text) is not None or _decimal_pair(text) is not None


def _unit_meters(text: str, unit: str) -> float:
    lowered = text.lower()
    if re.search(r"\b(km|kilometers?)\b", lowered):
        return 1000.0
    if re.search(r"\b(meters?|m)\b", lowered) and not re.search(r"\b(nm|nmi|nautical)\b", lowered):
        return 1.0
    if re.search(r"\b(nm|nmi|nautical)\b", lowered):
        return NAUTICAL_MILE_METERS
    name = unit.strip().lower()
    if name in {"km", "kilometer", "kilometers"}:
        return 1000.0
    if name in {"m", "meter", "meters"}:
        return 1.0
    return NAUTICAL_MILE_METERS


def _speed_scale(unit: str) -> float:
    name = unit.lower()
    if name == "mph":
        return 1609.344 / 3600
    if name in {"kts", "knots"}:
        return NAUTICAL_MILE_METERS / 3600
    if name in {"km/h", "kmh"}:
        return 1000 / 3600
    return 1.0


def _iso_date(text: str) -> bool:
    return "T" in text or bool(re.search(r"\d{4}-\d{2}-\d{2}", text))


def _near_clock(observed: datetime, hour: int, minute: int) -> datetime:
    candidate = observed.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate - observed > timedelta(hours=12):
        return candidate - timedelta(days=1)
    if observed - candidate > timedelta(hours=12):
        return candidate + timedelta(days=1)
    return candidate


def _on_day(observed: datetime, day: int, hour: int, minute: int) -> datetime | None:
    year = observed.year
    month = observed.month
    candidate = _dated(year, month, day, hour, minute)
    if candidate is None:
        return None
    if candidate - observed > timedelta(hours=12):
        month -= 1
        if month == 0:
            month = 12
            year -= 1
        candidate = _dated(year, month, day, hour, minute)
    elif observed - candidate > timedelta(hours=12):
        month += 1
        if month == 13:
            month = 1
            year += 1
        candidate = _dated(year, month, day, hour, minute)
    return candidate


def _dated(year: int, month: int, day: int, hour: int, minute: int) -> datetime | None:
    try:
        return datetime(year, month, day, hour, minute, tzinfo=UTC)
    except ValueError:
        return None


def _local_meters(
    latitude: float,
    longitude: float,
    origin_latitude: float,
    origin_longitude: float,
) -> tuple[float, float]:
    east = (
        math.radians(longitude - origin_longitude)
        * math.cos(math.radians(origin_latitude))
        * EARTH_RADIUS_METERS
    )
    north = math.radians(latitude - origin_latitude) * EARTH_RADIUS_METERS
    return east, north
