"""Resolve ChatSurfer rooms for one message type.

Precedence for ingest and for publish:

1. Coord card rooms on that TACREP type or template.
2. ``ANALYTICS_ROOMS_<TYPE>_INGEST`` or ``_PUBLISH``.
3. The card-level ``rooms`` section, when the card sets that side.
4. ``ANALYTICS_ROOMS_DEFAULT_INGEST`` or ``_PUBLISH``.

An empty result publishes nothing for that type. An explicit empty list
on the card (``ingest: []``) does not fall through.
"""

import re
from dataclasses import dataclass

_TYPE_KEY = re.compile(r"[^A-Z0-9]+")


@dataclass(frozen=True)
class TypeRooms:
    """Ingest and publish rooms for one TACREP type or template.

    ``None`` means that side was not set and the next precedence step
    applies. An empty tuple is set: nothing is read or published on
    that side, and the lookup does not fall through.
    """

    ingest: tuple[str, ...] | None = None
    publish: tuple[str, ...] | None = None


def type_key(name: str) -> str:
    """Normalize a type name for env keys and comparisons."""

    return _TYPE_KEY.sub("_", name.strip().upper()).strip("_")


def lookup_rooms(
    pairs: tuple[tuple[str, tuple[str, ...]], ...],
    type_name: str,
) -> tuple[str, ...] | None:
    """Return the configured list, or None when that type was not set."""

    target = type_key(type_name)
    for name, rooms in pairs:
        if type_key(name) == target:
            return rooms
    return None


def resolve_rooms(
    type_name: str,
    *,
    card_type: tuple[str, ...] | None,
    env_type: tuple[str, ...] | None,
    card_global: tuple[str, ...] | None,
    env_default: tuple[str, ...],
) -> tuple[str, ...]:
    """Apply the room precedence for one side (ingest or publish)."""

    if card_type is not None:
        return card_type
    if env_type is not None:
        return env_type
    if card_global is not None:
        return card_global
    return env_default


def parse_room_setting(value: str) -> tuple[str, ...] | None:
    """Read a comma-separated room list. Blank is unset. ``-`` is empty."""

    text = value.strip()
    if not text:
        return None
    if text == "-":
        return ()
    rooms: list[str] = []
    for part in text.replace("|", ",").replace(";", ",").split(","):
        name = part.strip()
        if name and name != "-" and name not in rooms:
            rooms.append(name)
    return tuple(rooms)
