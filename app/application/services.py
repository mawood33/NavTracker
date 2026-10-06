import asyncio
import logging

from app.domain.entities import TrackerPosition
from app.domain.ports import MessagePublisher, TrackerDataSource

logger = logging.getLogger(__name__)


class TrackerPollingService:
    def __init__(
        self,
        source: TrackerDataSource,
        publisher: MessagePublisher,
        tracker_ids: list[int],
        interval_seconds: int,
    ):
        self.source = source
        self.publisher = publisher
        self.tracker_ids = tracker_ids
        self.interval_seconds = interval_seconds

    async def run(self) -> None:
        while True:
            for tracker_id in self.tracker_ids:
                try:
                    position = await self.source.get_position(tracker_id)
                    await self.publisher.publish(position)
                except Exception:
                    logger.exception(
                        "Polling failed for tracker_id=%s",
                        tracker_id,
                    )

            await asyncio.sleep(self.interval_seconds)


class TrackerWebSocketService:
    def __init__(
        self,
        source: TrackerDataSource,
        publisher: MessagePublisher,
        tracker_ids: list[int],
    ):
        self.source = source
        self.publisher = publisher
        self.tracker_ids = tracker_ids

    async def run(self) -> None:
        while True:
            try:
                async for position in self.source.stream_positions(
                    self.tracker_ids
                ):
                    await self.publisher.publish(position)
            except Exception:
                logger.exception("Navixy WebSocket disconnected")
                await asyncio.sleep(5)
