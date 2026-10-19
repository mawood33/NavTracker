"""ChatSurfer tag rules and geoTag cross-check.

``SENT_BY_SYSTEM`` is never ingested. Our own nickname is never ingested,
so a publish does not come back as evidence. Other bot nicknames are
skipped only when configured. ``EXERCISE`` is kept as an attribute.
``MARKDOWN_FORMATTED`` is stripped before the coord-card parser runs.
Unknown tags are ignored.
"""

import re
from dataclasses import replace

from analytics.domain.correlation import TacrepMessage
from analytics.domain.geo import haversine_meters
from analytics.domain.timeutil import valid_position
from navtracker_common.chatsurfer import ChatRoomMessage

_LINK = re.compile(r"\[([^\]]+)\]\([^)]+\)")
_MARK = re.compile(r"[*_~`>#]")


def skip_message(
    message: ChatRoomMessage,
    *,
    nickname: str,
    skip_bots: tuple[str, ...],
    exercise_only: bool,
) -> bool:
    tags = set(message.tags)
    if "SENT_BY_SYSTEM" in tags:
        return True
    sender = message.nickname.strip()
    if nickname and sender == nickname:
        return True
    if sender and sender in skip_bots:
        return True
    if exercise_only and "EXERCISE" not in tags:
        return True
    return False


def strip_markdown(text: str) -> str:
    return _MARK.sub("", _LINK.sub(r"\1", text))


def prepare_body(message: ChatRoomMessage) -> str:
    if "MARKDOWN_FORMATTED" in message.tags:
        return strip_markdown(message.body)
    return message.body


def apply_geotag(
    message: TacrepMessage,
    points: tuple[tuple[float, float], ...],
    mismatch_meters: float,
) -> TacrepMessage:
    """Fill a missing position from the first geoTag, or warn on a mismatch."""

    if not points:
        return message
    latitude, longitude = points[0]
    if not valid_position(message.latitude, message.longitude):
        if not valid_position(latitude, longitude):
            return message
        return replace(message, latitude=latitude, longitude=longitude)
    if not valid_position(latitude, longitude):
        return message
    distance = haversine_meters(message.latitude, message.longitude, latitude, longitude)
    if distance <= mismatch_meters:
        return message
    note = "geotag disagrees with the parsed location"
    warning = f"{message.block_warning}; {note}" if message.block_warning else note
    return replace(message, block_warning=warning)


def tag_attributes(message: TacrepMessage, tags: tuple[str, ...]) -> TacrepMessage:
    if "EXERCISE" not in tags:
        return message
    return replace(message, attributes=(*message.attributes, ("tag", "EXERCISE")))
