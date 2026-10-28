from dataclasses import dataclass
from datetime import datetime
from typing import Any

from navtracker_common.timeutil import format_zulu

# Bump when the JSON keys or their meaning change. Consumers can branch
# on this field. Version 1 keeps every key, with null for an unknown value.
SCHEMA_VERSION = 1


@dataclass(frozen=True)
class OutboundMessage:
    """Versioned position document published to ChatSurfer."""

    schema_version: int
    tracker_id: int
    latitude: float
    longitude: float
    timestamp: datetime | None = None
    speed: float | None = None
    heading: float | None = None
    altitude: float | None = None
    connection_status: str | None = None
    movement_status: str | None = None

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "tracker_id": self.tracker_id,
            "latitude": self.latitude,
            "longitude": self.longitude,
            "timestamp": (
                format_zulu(self.timestamp)
                if self.timestamp is not None
                else None
            ),
            "speed": self.speed,
            "heading": self.heading,
            "altitude": self.altitude,
            "connection_status": self.connection_status,
            "movement_status": self.movement_status,
        }
