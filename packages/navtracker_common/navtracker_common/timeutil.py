"""UTC normalization shared by the worker and analytics."""

from datetime import UTC, datetime


def ensure_utc(moment: datetime) -> datetime:
    """Treat a naive timestamp as UTC. Aware timestamps convert to UTC."""

    if moment.tzinfo is None or moment.tzinfo.utcoffset(moment) is None:
        return moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC)
