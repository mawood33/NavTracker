from app.domain.entities import TrackerPosition
from app.domain.publication import SCHEMA_VERSION, OutboundMessage
from navtracker_common.units import DEFAULT_CHAT_SPEED_UNIT, speed_for_chat


def to_outbound_message(
    position: TrackerPosition,
    *,
    speed_unit: str = DEFAULT_CHAT_SPEED_UNIT,
) -> OutboundMessage:
    """Map a tracker fix to the versioned chat document.

    ``position.speed`` is meters per second. ``speed_unit`` is the
    ChatSurfer unit, ``kmh`` or ``mps``, shared with analytics.
    """

    return OutboundMessage(
        schema_version=SCHEMA_VERSION,
        tracker_id=position.tracker_id,
        latitude=position.latitude,
        longitude=position.longitude,
        timestamp=position.timestamp,
        speed=speed_for_chat(position.speed, speed_unit),
        heading=position.heading,
        altitude=position.altitude,
        connection_status=position.connection_status,
        movement_status=position.movement_status,
    )
