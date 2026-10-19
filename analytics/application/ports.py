"""Ports the analytics application needs from infrastructure.

Adapters live under ``analytics.infrastructure`` and are wired in
``analytics.composition``. This module does not import them.
"""

from collections.abc import Awaitable, Mapping, Sequence
from pathlib import Path
from typing import Protocol

from analytics.domain.columns import ColumnMap
from analytics.domain.reference import ArchiveLimits, Msr, ReferenceData
from analytics.domain.reports import Report
from analytics.domain.routing import RouteLeg, VehicleProfile
from analytics.domain.tracks import GpsFix


class ClairvoyantConnectionError(Exception):
    """The Clairvoyant socket or API could not be read."""


class ClairvoyantPoller(Protocol):
    async def poll(self) -> list[GpsFix]:
        """Return the latest positions from the API."""


class ClairvoyantSubscription(Protocol):
    async def receive(self) -> GpsFix | None:
        """Return the next fix, or None when the frame is not a position."""

    async def close(self) -> None:
        """Close the socket."""


class ClairvoyantStream(Protocol):
    async def connect(self) -> ClairvoyantSubscription:
        """Open a websocket subscription."""


class HexIndex(Protocol):
    def cell_id(self, latitude: float, longitude: float) -> str:
        """Index one point."""

    def disk(self, cell_id: str, k: int) -> tuple[str, ...]:
        """Return the cell and its neighbors out to ``k``."""

    def center(self, cell_id: str) -> tuple[float, float]:
        """Return the latitude and longitude of a cell."""

    def boundary(self, cell_id: str) -> tuple[tuple[float, float], ...]:
        """Return the cell ring as latitude, longitude pairs."""


class RouteEngine(Protocol):
    def route(
        self,
        origin: tuple[float, float],
        destination: tuple[float, float],
        profile: VehicleProfile,
        msrs: Sequence[Msr],
    ) -> RouteLeg:
        """Return one route. Transport failures become a domain error."""


class RoomPublisher(Protocol):
    async def publish_to(self, room: str, text: str) -> None:
        """Send one body to one ChatSurfer room.

        A failure is isolated: it is logged and dropped, and it does not
        surface as a room-read error.
        """


class ReferenceFiles(Protocol):
    def load_dir(
        self,
        directory: Path,
        *,
        columns: ColumnMap | None = None,
        limits: ArchiveLimits | None = None,
    ) -> ReferenceData:
        """Read a directory, skipping files that do not parse."""

    def parse_bytes(
        self,
        filename: str,
        data: bytes,
        *,
        columns: ColumnMap | None = None,
        limits: ArchiveLimits | None = None,
    ) -> ReferenceData:
        """Parse one upload in memory."""


class ReportPoller(Protocol):
    def poll(self) -> Awaitable[list[object]]:
        """Return newly observed reports."""


class ReportSource(Protocol):
    async def poll(self) -> list[Report]:
        """Return newly observed reports. Does not have to be called at import."""


class DocumentSink(Protocol):
    async def publish(self, document: Mapping[str, object]) -> None:
        """Deliver one snapshot or diff. Called only after analysis."""


class ChatConnectionError(Exception):
    """The ChatSurfer socket could not be read."""


class ChatSubscription(Protocol):
    async def receive(self) -> tuple[str, object] | None:
        """``("message", ChatRoomMessage)``, ``("scrub", id)``, or a heart-beat."""

    async def close(self) -> None:
        """Release the socket."""


class ChatEventStream(Protocol):
    async def connect(self) -> ChatSubscription:
        """Open STOMP and subscribe to the configured rooms."""
