"""Log one tracker fix before it is resolved, deduped, or published.

The logger is ``navtracker.positions``. ``LOG_POSITIONS`` selects INFO
(the default), DEBUG, or quiet DEBUG records. Lines go to the root
handlers, which are stdout and the rotating file. The message names
only the fix fields. It does not include API keys or encrypted payloads.
"""

import logging
from datetime import datetime

from app.domain.entities import TrackerPosition
from navtracker_common.logging_setup import POSITIONS_LOGGER, position_logging
from navtracker_common.timeutil import ensure_utc, format_zulu

positions = logging.getLogger(POSITIONS_LOGGER)


def log_position(
    position: TrackerPosition,
    *,
    source: str,
    compared_to: datetime | None,
    normalized: datetime | None,
    decision: str,
) -> None:
    """Record the fix as received and the decision about to be applied."""

    raw = position.received_timestamp
    if raw is None:
        raw = position.timestamp
    previous_utc = None if compared_to is None else _as_utc(compared_to)
    level, _unused_logger_level = position_logging()
    positions.log(
        level,
        "tracker_id=%s source=%s raw_timestamp=%s raw_type=%s raw_tzinfo=%s "
        "normalized_utc=%s latitude=%s longitude=%s speed=%s heading=%s "
        "previous=%s previous_type=%s previous_tzinfo=%s previous_utc=%s "
        "decision=%s",
        position.tracker_id,
        source,
        _rendered(raw),
        _type_name(raw),
        _tzinfo_name(raw),
        _rendered(normalized),
        position.latitude,
        position.longitude,
        position.speed,
        position.heading,
        _rendered(compared_to),
        _type_name(compared_to),
        _tzinfo_name(compared_to),
        _rendered(previous_utc),
        decision,
    )


def _as_utc(moment: datetime) -> datetime:
    return ensure_utc(moment)


def _rendered(value: datetime | None) -> str:
    if value is None:
        return "None"
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        return value.isoformat()
    return format_zulu(value)


def _type_name(value: object) -> str:
    if value is None:
        return "NoneType"
    return type(value).__name__


def _tzinfo_name(value: object) -> str:
    if not isinstance(value, datetime):
        return "None"
    tz = value.tzinfo
    if tz is None or tz.utcoffset(value) is None:
        return "None"
    name = tz.tzname(value)
    if name:
        return name
    return repr(tz)
