"""CAT is a target-location-error category, not a confidence score.

A lower category number is more accurate. CAT I is the tightest.
Each category maps to one circular-error radius. The built-in radii are
the common TLE upper bounds and stay configurable:

- CAT I: 6 m
- CAT II: 15 m
- CAT III: 30 m
- CAT IV: 91 m

Config and the coord card name the radius and its unit (``m``, ``km``,
or ``nmi``). Stored radii are meters.
"""

import re

_NAUTICAL_MILE_METERS = 1852.0

DEFAULT_CAT_CE_METERS: tuple[tuple[int, float], ...] = (
    (1, 6.0),
    (2, 15.0),
    (3, 30.0),
    (4, 91.0),
)

_PAIR = re.compile(
    r"([A-Za-z]+|\d+)\s*[:=]\s*(\d+(?:\.\d+)?)\s*([A-Za-z]*)",
)


class CatTableError(ValueError):
    """The CAT-to-CE table cannot be read."""


def scale_to_meters(unit: str) -> float:
    """Return the multiplier that converts ``unit`` into meters."""

    text = unit.strip().lower()
    if text in {"m", "meter", "meters"}:
        return 1.0
    if text in {"km", "kilometer", "kilometers"}:
        return 1000.0
    if text in {"nmi", "nm", "nautical", "nautical_miles"}:
        return _NAUTICAL_MILE_METERS
    raise CatTableError(f"Unknown length unit {unit!r}. Expected m, km, or nmi.")


def parse_cat_ce(
    value: object,
    *,
    default_unit: str = "m",
) -> tuple[tuple[int, float], ...]:
    """Read a CAT-to-CE table and return ``(level, radius_m)`` pairs."""

    if value is None or value == "":
        return DEFAULT_CAT_CE_METERS
    if isinstance(value, str):
        return _from_text(value, default_unit)
    if isinstance(value, dict):
        return _from_mapping(value, default_unit)
    raise CatTableError("cat_ce must be a mapping or a compact list.")


def radius_for(table: tuple[tuple[int, float], ...], level: int) -> float | None:
    """Return the circular-error radius in meters for one CAT level."""

    for cat, radius in table:
        if cat == level:
            return radius
    return None


def _from_text(text: str, default_unit: str) -> tuple[tuple[int, float], ...]:
    found: list[tuple[int, float]] = []
    seen: set[int] = set()
    for match in _PAIR.finditer(text):
        level = _level(match.group(1))
        unit = match.group(3) or default_unit
        radius = float(match.group(2)) * scale_to_meters(unit)
        _add(found, seen, level, radius)
    if not found:
        raise CatTableError("cat_ce needs at least one category.")
    return tuple(found)


def _from_mapping(
    raw: dict[str, object],
    default_unit: str,
) -> tuple[tuple[int, float], ...]:
    unit = str(raw.get("unit", default_unit) or default_unit)
    body = raw.get("radii", raw)
    if not isinstance(body, dict):
        raise CatTableError("cat_ce radii must be a mapping.")
    found: list[tuple[int, float]] = []
    seen: set[int] = set()
    for key, value in body.items():
        name = str(key).strip().lower()
        if name in {"unit", "radii"}:
            continue
        level = _level(key)
        radius, item_unit = _radius_token(value, unit)
        _add(found, seen, level, radius * scale_to_meters(item_unit))
    if not found:
        raise CatTableError("cat_ce needs at least one category.")
    return tuple(found)


def _level(value: object) -> int:
    if isinstance(value, bool):
        raise CatTableError(f"Unknown CAT level {value!r}.")
    if isinstance(value, int):
        if value <= 0:
            raise CatTableError(f"Unknown CAT level {value!r}.")
        return value
    from analytics.domain.positions import parse_cat

    level = parse_cat(str(value))
    if level is None:
        raise CatTableError(f"Unknown CAT level {value!r}.")
    return level


def _radius_token(value: object, default_unit: str) -> tuple[float, str]:
    if isinstance(value, bool) or isinstance(value, (int, float)):
        radius = float(value)
        if radius <= 0:
            raise CatTableError("CE radius must be greater than 0.")
        return radius, default_unit
    text = str(value).strip()
    match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([A-Za-z]*)", text)
    if match is None:
        raise CatTableError(f"CE radius {value!r} is not a length.")
    radius = float(match.group(1))
    if radius <= 0:
        raise CatTableError("CE radius must be greater than 0.")
    return radius, match.group(2) or default_unit


def _add(
    found: list[tuple[int, float]],
    seen: set[int],
    level: int,
    radius: float,
) -> None:
    if level in seen:
        raise CatTableError(f"CAT {level} is listed twice.")
    if radius <= 0:
        raise CatTableError("CE radius must be greater than 0.")
    seen.add(level)
    found.append((level, radius))
    found.sort()
