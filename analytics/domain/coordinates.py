"""ChatSurfer output coordinates.

Input parsing still accepts decimal degrees, MGRS, and hemisphere
``DDMM.MM``. Published text uses a format from this registry. ``mgrs``
and decimal ``latlon`` are the forms ChatSurfer maps. ``ddmm`` is
accepted only when a card, environment, or analyst asks for it.

A new format is ``register_coordinate_format(name, function)``.
"""

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace

from analytics.domain.coordcard import MgrsGrid
from analytics.domain.geo import haversine_meters
from analytics.domain.positions import parse_location
from analytics.domain.timeutil import valid_position

_FORMATS: dict[str, Callable[..., "FormattedPoint"]] = {}
_PRECISION_DIGITS = {1: 5, 10: 4, 100: 3}
_TACREP_TYPES = frozenset({"TACREP", "MTI", "IMINT", "ELINT", "FUSED"})
_UTM = re.compile(r"(\d{1,2}[C-HJ-NP-X])([A-HJ-NP-Z]{2})(\d+)")
_UPS = re.compile(r"([ABYZ])([A-HJ-NP-Z]{2})(\d+)")
_SUFFIX = re.compile(
    r"([+-]?\d+(?:\.\d+)?)([NnSs])\s*[, ]\s*([+-]?\d+(?:\.\d+)?)([EeWw])"
)


class CoordinateFormatError(ValueError):
    """The output coordinate settings cannot be read."""


@dataclass(frozen=True)
class OutputCoordinates:
    """One resolved output format. ``by_type`` is empty on a type entry."""

    format: str = "mgrs"
    precision_m: int = 1
    spaces: bool = False
    decimals: int = 5
    separator: str = " "
    hemisphere: str = "suffix"
    by_type: tuple[tuple[str, "OutputCoordinates"], ...] = ()


@dataclass(frozen=True)
class FormattedPoint:
    """Published text and a warning when the requested form was not used."""

    text: str
    warning: str = ""


def register_coordinate_format(
    name: str,
    formatter: Callable[[float, float, OutputCoordinates, MgrsGrid | None], FormattedPoint],
) -> None:
    """Add a format. The name is matched case-insensitively."""

    key = name.strip().lower()
    if not key:
        raise CoordinateFormatError("A coordinate format needs a name.")
    _FORMATS[key] = formatter


def known_coordinate_formats() -> tuple[str, ...]:
    return tuple(sorted(_FORMATS))


def output_from_mapping(value: object) -> OutputCoordinates:
    """Read ``output`` from a coord card or an analyst override."""

    if not isinstance(value, dict):
        raise CoordinateFormatError("output coordinates must be a mapping.")
    base = _scalars(value, OutputCoordinates())
    raw_types = value.get("types", {})
    if raw_types in (None, ""):
        raw_types = {}
    if not isinstance(raw_types, dict):
        raise CoordinateFormatError("output.types must be a mapping.")
    typed: list[tuple[str, OutputCoordinates]] = []
    for name, item in raw_types.items():
        label = str(name).strip().upper()
        if not label:
            raise CoordinateFormatError("output.types needs a type name.")
        if not isinstance(item, dict):
            raise CoordinateFormatError(f"output type {label} must be a mapping.")
        typed.append((label, _scalars(item, base)))
    return replace(base, by_type=tuple(typed))


def resolve_output_coordinates(
    override: OutputCoordinates | None,
    card: OutputCoordinates | None,
    env: OutputCoordinates,
    *,
    env_configured: bool,
    message_type: str = "",
) -> tuple[OutputCoordinates, str]:
    """Analyst override, then the coord card, then env, then ``mgrs``."""

    if override is not None:
        return _for_type(override, message_type), "override"
    if card is not None:
        return _for_type(card, message_type), "coord_card"
    if env_configured:
        return _for_type(env, message_type), "env"
    return _for_type(env, message_type), "default"


def format_point(
    latitude: float,
    longitude: float,
    spec: OutputCoordinates,
    grid: MgrsGrid | None = None,
) -> FormattedPoint:
    """Format one point. An unknown or failed MGRS request falls back to latlon."""

    if not valid_position(latitude, longitude):
        return FormattedPoint("", "coordinates are outside the valid range")
    formatter = _FORMATS.get(spec.format.strip().lower())
    if formatter is None:
        fallback = _format_latlon(latitude, longitude, spec, grid)
        return FormattedPoint(
            fallback.text,
            f"unknown output coordinate format {spec.format!r}; using decimal lat/lon",
        )
    return formatter(latitude, longitude, spec, grid)


def parse_formatted(
    text: str,
    grid: MgrsGrid | None = None,
) -> tuple[float, float] | None:
    """Read a value this module emitted, plus the input forms already accepted."""

    body = " ".join(text.split())
    if not body:
        return None
    suffix = _SUFFIX.fullmatch(body.replace(",", " "))
    if suffix is not None:
        latitude = float(suffix.group(1))
        longitude = float(suffix.group(3))
        if suffix.group(2).upper() == "S":
            latitude = -abs(latitude)
        elif suffix.group(2).upper() == "N":
            latitude = abs(latitude)
        if suffix.group(4).upper() == "W":
            longitude = -abs(longitude)
        elif suffix.group(4).upper() == "E":
            longitude = abs(longitude)
        if valid_position(latitude, longitude):
            return latitude, longitude
    parsed = parse_location(text, grid)
    if parsed is not None:
        return parsed
    compact = "".join(text.split())
    if grid is None:
        return None
    return grid.to_latlon(compact)


def rewrite_published_location(raw: str, location_text: str, formatted: str) -> str:
    """Replace the received location in a copy of the message. ``raw`` stays stored."""

    note = formatted.strip()
    token = location_text.strip()
    if not raw or not token or not note or "//" in note:
        return raw
    if token not in raw:
        return raw
    return raw.replace(token, note, 1)


def coordinate_document(
    spec: OutputCoordinates,
    *,
    source: str,
    message_type: str = "",
) -> dict[str, object]:
    """Fields an analyst UI can show."""

    return {
        "format": spec.format,
        "precision_m": spec.precision_m,
        "spaces": spec.spaces,
        "decimals": spec.decimals,
        "separator": spec.separator,
        "hemisphere": spec.hemisphere,
        "source": source,
        "message_type": message_type,
        "types": {
            name: coordinate_document(item, source=source, message_type=name)
            for name, item in spec.by_type
        },
    }


def within_precision(
    left: tuple[float, float],
    right: tuple[float, float],
    spec: OutputCoordinates,
) -> bool:
    """True when two points agree at the configured output precision."""

    distance = haversine_meters(left[0], left[1], right[0], right[1])
    if spec.format == "mgrs":
        # A grid square can place the parsed point about one cell diagonally away.
        return distance <= float(spec.precision_m) * 1.5 + 1.0
    if spec.format == "ddmm":
        return distance <= 2000.0
    step = float(10 ** (-max(spec.decimals, 0))) * 111_320.0
    return distance <= max(step, 1.0)


def _for_type(spec: OutputCoordinates, message_type: str) -> OutputCoordinates:
    name = message_type.strip().upper()
    if not name:
        return spec
    table = {key.upper(): value for key, value in spec.by_type}
    chosen = table.get(name)
    if chosen is None and name in _TACREP_TYPES:
        chosen = table.get("TACREP")
    if chosen is None:
        return spec
    return chosen


def _scalars(value: Mapping[str, object], defaults: OutputCoordinates) -> OutputCoordinates:
    format_name = value.get("coordinate_format", value.get("format", defaults.format))
    name = str(format_name or "").strip().lower()
    if name not in _FORMATS:
        choices = ", ".join(sorted(_FORMATS))
        raise CoordinateFormatError(
            f"Unknown coordinate format {format_name!r}. Expected one of: {choices}."
        )
    precision = _precision(
        value.get("mgrs_precision_m", value.get("precision_m", defaults.precision_m))
    )
    spaces = _bool(value.get("mgrs_spaces", value.get("spaces", defaults.spaces)), "mgrs_spaces")
    decimals = _decimals(value.get("latlon_decimals", value.get("decimals", defaults.decimals)))
    separator = _separator(
        value.get("latlon_separator", value.get("separator", defaults.separator))
    )
    hemisphere = _hemisphere(
        value.get("latlon_hemisphere", value.get("hemisphere", defaults.hemisphere))
    )
    return OutputCoordinates(
        format=name,
        precision_m=precision,
        spaces=spaces,
        decimals=decimals,
        separator=separator,
        hemisphere=hemisphere,
    )


def _precision(value: object) -> int:
    number = _as_int(value, "mgrs_precision_m must be 1, 10, or 100.")
    if number not in _PRECISION_DIGITS:
        raise CoordinateFormatError("mgrs_precision_m must be 1, 10, or 100.")
    return number


def _decimals(value: object) -> int:
    number = _as_int(value, "latlon_decimals must be an integer from 0 to 8.")
    if number < 0 or number > 8:
        raise CoordinateFormatError("latlon_decimals must be an integer from 0 to 8.")
    return number


def _as_int(value: object, message: str) -> int:
    if isinstance(value, bool):
        raise CoordinateFormatError(message)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not value.is_integer():
            raise CoordinateFormatError(message)
        return int(value)
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError as exc:
            raise CoordinateFormatError(message) from exc
    raise CoordinateFormatError(message)


def _separator(value: object) -> str:
    text = str(value if value is not None else "")
    token = text.strip().lower()
    if token in {"space", "blank"}:
        return " "
    if token in {"comma", ","}:
        return ","
    if text in {" ", ","}:
        return text
    raise CoordinateFormatError("latlon_separator must be a space or a comma.")


def _hemisphere(value: object) -> str:
    text = str(value or "").strip().lower().replace("_", "-")
    if text in {"suffix", "hemisphere-suffix"}:
        return "suffix"
    if text in {"signed", "signed-decimal"}:
        return "signed"
    raise CoordinateFormatError("latlon_hemisphere must be suffix or signed.")


def _bool(value: object, name: str) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "yes"}:
        return True
    if text in {"0", "false", "no"}:
        return False
    raise CoordinateFormatError(f"{name} must be true or false.")


def _format_mgrs(
    latitude: float,
    longitude: float,
    spec: OutputCoordinates,
    grid: MgrsGrid | None,
) -> FormattedPoint:
    encoded = None
    if grid is not None:
        encoded = grid.to_mgrs(latitude, longitude, spec.precision_m)
    if not encoded:
        fallback = _format_latlon(latitude, longitude, spec, grid)
        return FormattedPoint(
            fallback.text,
            "MGRS conversion failed; using decimal lat/lon",
        )
    text = _space_mgrs(encoded) if spec.spaces else encoded
    return FormattedPoint(text)


def _format_latlon(
    latitude: float,
    longitude: float,
    spec: OutputCoordinates,
    grid: MgrsGrid | None,
) -> FormattedPoint:
    del grid
    if spec.hemisphere == "signed":
        left = f"{latitude:.{spec.decimals}f}"
        right = f"{longitude:.{spec.decimals}f}"
    else:
        left = _suffix_number(latitude, "N", "S", spec.decimals)
        right = _suffix_number(longitude, "E", "W", spec.decimals)
    return FormattedPoint(f"{left}{spec.separator}{right}")


def _format_ddmm(
    latitude: float,
    longitude: float,
    spec: OutputCoordinates,
    grid: MgrsGrid | None,
) -> FormattedPoint:
    del grid
    left = _ddmm_token(latitude, latitude >= 0, "N", "S", spec.decimals)
    right = _ddmm_token(longitude, longitude >= 0, "E", "W", spec.decimals)
    return FormattedPoint(
        f"{left} {right}",
        "output coordinate format ddmm is not recognized as a ChatSurfer point",
    )


def _suffix_number(value: float, positive: str, negative: str, decimals: int) -> str:
    letter = positive if value >= 0 else negative
    return f"{abs(value):.{decimals}f}{letter}"


def _ddmm_token(
    value: float,
    positive_value: bool,
    positive: str,
    negative: str,
    decimals: int,
) -> str:
    letter = positive if positive_value else negative
    magnitude = abs(value)
    degrees = int(magnitude)
    minutes = (magnitude - degrees) * 60
    if decimals <= 0:
        whole = int(round(minutes))
        if whole >= 60:
            degrees += 1
            whole = 0
        return f"{letter}{degrees}{whole:02d}"
    # Minutes stay two digits so 5°0' is E500.00, which the input parser can read.
    width = decimals + 3
    return f"{letter}{degrees}{minutes:0{width}.{decimals}f}"


def _space_mgrs(compact: str) -> str:
    match = _UTM.fullmatch(compact) or _UPS.fullmatch(compact)
    if match is None:
        return compact
    zone, square, digits = match.groups()
    if len(digits) % 2:
        return compact
    half = len(digits) // 2
    return f"{zone} {square} {digits[:half]} {digits[half:]}"


register_coordinate_format("mgrs", _format_mgrs)
register_coordinate_format("latlon", _format_latlon)
register_coordinate_format("ddmm", _format_ddmm)
