"""UTC normalization and coordinate checks used at ingestion."""

from datetime import UTC, datetime


def ensure_utc(moment: datetime) -> datetime:
    """Treat a naive timestamp as UTC. Aware timestamps convert to UTC."""

    if moment.tzinfo is None:
        return moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC)


def valid_position(latitude: float, longitude: float) -> bool:
    """Latitude is -90..90 and longitude is -180..180."""

    return -90.0 <= latitude <= 90.0 and -180.0 <= longitude <= 180.0
