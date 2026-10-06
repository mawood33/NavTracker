from dataclasses import dataclass
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class TrackerPosition:
    tracker_id: int
    latitude: float
    longitude: float
    timestamp: datetime
    speed: float | None = None
    heading: float | None = None
    altitude: float | None = None
    connection_status: str | None = None
    movement_status: str | None = None
    raw: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "tracker_id": self.tracker_id,
            "latitude": self.latitude,
            "longitude": self.longitude,
            "timestamp": self.timestamp.isoformat(),
            "speed": self.speed,
            "heading": self.heading,
            "altitude": self.altitude,
            "connection_status": self.connection_status,
            "movement_status": self.movement_status,
        }
