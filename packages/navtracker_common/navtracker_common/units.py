"""Unit conversions shared by ingest and analytics.

Navixy ``gps.speed`` is kilometers per hour. Stored speeds and the
``speed_mps`` field are meters per second.
"""


def kmh_to_mps(kilometers_per_hour: float) -> float:
    """Convert kilometers per hour to meters per second."""

    return kilometers_per_hour / 3.6
