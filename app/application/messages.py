from app.domain.entities import TrackerPosition
from app.domain.publication import SCHEMA_VERSION, OutboundMessage


def to_outbound_message(position: TrackerPosition) -> OutboundMessage:
    """Map a tracker fix to the versioned chat document."""

    return OutboundMessage(
        schema_version=SCHEMA_VERSION,
        tracker_id=position.tracker_id,
        latitude=position.latitude,
        longitude=position.longitude,
        timestamp=position.timestamp,
        speed=position.speed,
        heading=position.heading,
        altitude=position.altitude,
        connection_status=position.connection_status,
        movement_status=position.movement_status,
    )
