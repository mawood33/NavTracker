from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class TrackerPosition:
    """A tracker fix. This is not the ChatSurfer payload.

    ``received_timestamp`` is the provider value before UTC normalization.
    It is diagnostic only and is not published.
    """

    tracker_id: int
    latitude: float
    longitude: float
    timestamp: datetime | None = None
    speed: float | None = None
    heading: float | None = None
    altitude: float | None = None
    connection_status: str | None = None
    movement_status: str | None = None
    received_timestamp: datetime | None = None
