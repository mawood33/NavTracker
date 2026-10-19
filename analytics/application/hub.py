"""In-process fan-out for analytics documents. Infrastructure frames use this."""

import asyncio
from collections.abc import Mapping


class ClientQueue(asyncio.Queue[Mapping[str, object]]):
    """A bounded queue. ``dropped`` is set when a slow consumer misses a document."""

    def __init__(self, maxsize: int) -> None:
        super().__init__(maxsize=maxsize)
        self.dropped = asyncio.Event()

    def put_nowait(self, item: Mapping[str, object]) -> None:
        try:
            super().put_nowait(item)
        except asyncio.QueueFull:
            self.dropped.set()


class OutputHub:
    """Fan-out for one process. Subscribers receive documents already built."""

    def __init__(self) -> None:
        self._snapshot: dict[str, object] | None = None
        self._queues: list[ClientQueue] = []

    def latest_snapshot(self) -> dict[str, object] | None:
        return self._snapshot

    def set_snapshot(self, document: dict[str, object]) -> None:
        self._snapshot = document

    def subscribe(self, maxsize: int = 8) -> ClientQueue:
        queue = ClientQueue(maxsize)
        self._queues.append(queue)
        return queue

    def unsubscribe(self, queue: ClientQueue) -> None:
        if queue in self._queues:
            self._queues.remove(queue)

    def broadcast(self, document: Mapping[str, object]) -> None:
        for queue in list(self._queues):
            queue.put_nowait(document)
