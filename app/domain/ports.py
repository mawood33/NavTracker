from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from app.domain.entities import TrackerPosition
from app.domain.publication import OutboundMessage


class PositionReader(Protocol):
    """Request/response port for API polling."""

    async def get_position(self, tracker_id: int) -> TrackerPosition:
        """Return the current position for one tracker."""


class TrackerCatalog(Protocol):
    """Read the tracker ids visible to the Navixy API key."""

    async def list_tracker_ids(self) -> list[int]:
        """Return tracker ids from the tracker list.

        An empty list means the account has no trackers. It is not a
        request to subscribe to every tracker.
        Raises TrackerDiscoveryError when the list cannot be read.
        """


class PositionSubscription(Protocol):
    """One open Navixy socket subscribed to a fixed tracker set."""

    async def receive(self) -> TrackerPosition | None:
        """Return the next in-scope position.

        None means the socket is still up and there is nothing to publish
        (idle read, unusable frame, or a tracker outside the subscription).
        Raises SourceConnectionError when the socket fails.
        """

    async def close(self) -> None:
        """Release the socket."""


class PositionStream(Protocol):
    """Streaming port. Implementations subscribe only to the given ids."""

    async def connect(self, tracker_ids: list[int]) -> PositionSubscription:
        """Open a socket for tracker_ids.

        Raises SourceConnectionError when the connection cannot be opened.
        Raises ValueError when tracker_ids is empty so callers cannot fall
        through to a subscription for every tracker.
        """


class MessagePublisher(Protocol):
    async def publish(self, message: OutboundMessage) -> dict[str, Any]:
        """Publish one versioned position document."""


PositionHandler = Callable[[TrackerPosition], Awaitable[None]]
