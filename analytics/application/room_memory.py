"""Bounded per-room message ids and revision high-water marks."""

from dataclasses import dataclass, field
from datetime import datetime

from navtracker_common.chatsurfer import ChatRoomMessage


@dataclass
class RoomMemory:
    revisions: dict[str, str] = field(default_factory=dict)
    order: list[str] = field(default_factory=list)
    high_water: datetime | None = None
    last_id: str = ""
    backfilled: bool = False


def note_message(memory: RoomMemory, message: ChatRoomMessage, limit: int) -> str:
    """Return ``new``, ``edit``, or ``duplicate``.

    The same id and revision is a duplicate. ``isEdited`` with a new
    ``revisionId`` supersedes the earlier revision.
    """

    if not message.message_id:
        return "new"
    revision = message.revision_id or message.message_id
    previous = memory.revisions.get(message.message_id)
    if previous == revision:
        return "duplicate"
    memory.revisions[message.message_id] = revision
    if previous is None:
        memory.order.append(message.message_id)
        while limit > 0 and len(memory.order) > limit:
            dropped = memory.order.pop(0)
            memory.revisions.pop(dropped, None)
        _touch(memory, message)
        return "new"
    if message.is_edited:
        _touch(memory, message)
        return "edit"
    return "duplicate"


def _touch(memory: RoomMemory, message: ChatRoomMessage) -> None:
    if message.timestamp is not None and (
        memory.high_water is None or message.timestamp >= memory.high_water
    ):
        memory.high_water = message.timestamp
        memory.last_id = message.message_id
        return
    if message.message_id:
        memory.last_id = message.message_id
