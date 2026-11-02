"""Unit conversions shared by ingest and analytics.

Navixy ``gps.speed`` is kilometers per hour. Stored speeds and the
``speed_mps`` field are meters per second. The ChatSurfer ``speed``
field uses ``ANALYTICS_CHAT_SPEED_UNIT``: ``kmh`` (the default) or
``mps``. Ingest and analytics both read that variable.
"""

CHAT_SPEED_UNITS = frozenset({"kmh", "mps"})
DEFAULT_CHAT_SPEED_UNIT = "kmh"


def kmh_to_mps(kilometers_per_hour: float) -> float:
    """Convert kilometers per hour to meters per second."""

    return kilometers_per_hour / 3.6


def parse_chat_speed_unit(value: object, *, name: str = "ANALYTICS_CHAT_SPEED_UNIT") -> str:
    """Return ``kmh`` or ``mps``. Anything else fails startup."""

    text = str(value).strip().lower()
    if text not in CHAT_SPEED_UNITS:
        choices = ", ".join(sorted(CHAT_SPEED_UNITS))
        raise RuntimeError(f"Invalid {name}={value!r}. Expected one of: {choices}.")
    return text


def speed_for_chat(speed_mps: float | None, unit: str) -> float | None:
    """Convert an internal meters-per-second speed into the ChatSurfer unit."""

    if speed_mps is None:
        return None
    chosen = parse_chat_speed_unit(unit)
    if chosen == "kmh":
        return speed_mps * 3.6
    return speed_mps
