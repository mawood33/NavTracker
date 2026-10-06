from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Protocol

from .entities import TrackerPosition


class TrackerDataSource(Protocol):
    async def get_position(self, tracker_id: int) -> TrackerPosition:
        ...

    async def stream_positions(
        self,
        tracker_ids: list[int],
    ) -> AsyncIterator[TrackerPosition]:
        ...


class MessagePublisher(Protocol):
    async def publish(self, position: TrackerPosition) -> dict:
        ...


PositionHandler = Callable[[TrackerPosition], Awaitable[None]]
