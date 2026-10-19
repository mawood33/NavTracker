"""Facade over the focused analytics services.

``wire_service`` builds the pieces and passes them here. Callers keep
using this type, including the attributes tests set on the catalog.
"""

from collections.abc import Mapping
from datetime import datetime

from analytics.application.analysis import AnalysisService
from analytics.application.catalog import CatalogService
from analytics.application.hub import OutputHub
from analytics.application.ingest import IngestService
from analytics.application.loop import AnalysisLoop
from analytics.application.ports import DocumentSink, ReportSource
from analytics.application.publishing import PublishService
from analytics.application.query import QueryService
from analytics.application.routing import RoutingService
from analytics.application.settings import AnalyticsSettings
from analytics.application.state import AnalyticsState
from analytics.domain.access import AuthPolicy
from analytics.domain.aoi import AreaOfInterest
from analytics.domain.correlation import TacrepMessage
from analytics.domain.grid import CellActivity
from analytics.domain.reference import Msr, Site
from analytics.domain.reports import Report
from analytics.domain.tracks import GpsFix

__all__ = ["AnalyticsService", "DocumentSink", "ReportSource"]

_OWN = {
    "_state",
    "_query",
    "_ingest",
    "_routing",
    "_catalog",
    "_publish",
    "_analysis",
    "_loop",
}


class AnalyticsService:
    """Application entry. Each method delegates to one focused service."""

    _state: AnalyticsState
    _query: QueryService
    _ingest: IngestService
    _routing: RoutingService
    _catalog: CatalogService
    _publish: PublishService
    _analysis: AnalysisService
    _loop: AnalysisLoop

    def __init__(
        self,
        state: AnalyticsState,
        *,
        query: QueryService,
        ingest: IngestService,
        routing: RoutingService,
        catalog: CatalogService,
        publish: PublishService,
        analysis: AnalysisService,
        loop: AnalysisLoop,
    ) -> None:
        object.__setattr__(self, "_state", state)
        object.__setattr__(self, "_query", query)
        object.__setattr__(self, "_ingest", ingest)
        object.__setattr__(self, "_routing", routing)
        object.__setattr__(self, "_catalog", catalog)
        object.__setattr__(self, "_publish", publish)
        object.__setattr__(self, "_analysis", analysis)
        object.__setattr__(self, "_loop", loop)

    @property
    def settings(self) -> AnalyticsSettings:
        return self._state.settings

    @property
    def hub(self) -> OutputHub:
        return self._state.hub

    def __getattr__(self, name: str) -> object:
        state = object.__getattribute__(self, "_state")
        if hasattr(state, name):
            return getattr(state, name)
        raise AttributeError(name)

    def __setattr__(self, name: str, value: object) -> None:
        if name in _OWN or not hasattr(self, "_state"):
            object.__setattr__(self, name, value)
            return
        state = object.__getattribute__(self, "_state")
        if hasattr(state, name):
            setattr(state, name, value)
            return
        object.__setattr__(self, name, value)

    def load_reference(self) -> dict[str, int]:
        return self._catalog.load_reference()

    def reload_reference(self) -> dict[str, int]:
        return self._catalog.reload_reference()

    def save_upload(self, filename: str, body: bytes) -> dict[str, int]:
        return self._catalog.save_upload(filename, body)

    def save_coord_card(
        self,
        filename: str,
        body: bytes,
        content_type: str = "",
    ) -> dict[str, int]:
        return self._catalog.save_coord_card(filename, body, content_type)

    def save_coord_card_schema(self, body: bytes, version: str = "") -> dict[str, object]:
        return self._catalog.save_coord_card_schema(body, version)

    def add_fixes(self, fixes: list[GpsFix]) -> None:
        self._ingest.add_fixes(fixes)

    def add_reports(self, reports: list[Report]) -> None:
        self._ingest.add_reports(reports)

    def add_messages(self, messages: list[TacrepMessage]) -> None:
        self._ingest.add_messages(messages)

    def analyze(self) -> None:
        self._analysis.analyze()

    def health(self) -> dict[str, object]:
        return self._query.health()

    def events(self) -> list[object]:
        return self._query.events()

    def areas(self) -> list[AreaOfInterest]:
        return self._query.areas()

    def sites(self) -> list[Site]:
        return self._query.sites()

    def routes(self) -> list[Msr]:
        return self._query.routes()

    def cells(self) -> list[CellActivity]:
        return self._query.cells()

    def corridor_meters(self) -> float:
        return self._query.corridor_meters()

    def msr_reports(self) -> dict[str, tuple[Report, ...]]:
        return self._query.msr_reports()

    def tracks(self) -> list[GpsFix]:
        return self._query.tracks()

    def public_base(self) -> str:
        return self._query.public_base()

    def basemap(self) -> dict[str, object]:
        return self._query.basemap()

    def routing_info(self) -> dict[str, object]:
        return self._query.routing_info()

    def route_between(
        self,
        origin_name: str,
        destination_name: str,
        profile_name: str,
    ) -> dict[str, object]:
        return self._routing.route_between(origin_name, destination_name, profile_name)

    def output_snapshot(self, as_of: datetime | None = None) -> dict[str, object]:
        return self._query.output_snapshot(as_of)

    def api_sites(self) -> list[dict[str, object]]:
        return self._query.api_sites()

    def api_route(
        self,
        origin_id: str,
        destination_id: str,
        vehicle_type: str,
        max_speed_mph: float | None,
    ) -> dict[str, object]:
        return self._routing.api_route(origin_id, destination_id, vehicle_type, max_speed_mph)

    def ingest_event(
        self,
        payload: Mapping[str, object],
        source_id: str = "",
    ) -> dict[str, object]:
        return self._ingest.ingest_event(payload, source_id)

    def ingest_tacrep_body(self, body: str, source_id: str) -> dict[str, object]:
        return self._ingest.ingest_tacrep_body(body, source_id)

    def ingest_batch(
        self,
        payload: Mapping[str, object],
        source_id: str = "",
    ) -> dict[str, object]:
        return self._ingest.ingest_batch(payload, source_id)

    def correlations_document(self) -> dict[str, object]:
        return self._query.correlations_document()

    def activities_document(self, as_of: datetime | None = None) -> dict[str, object]:
        return self._query.activities_document(as_of)

    def advance_activity(
        self,
        activity_id: str,
        as_of: datetime | None = None,
    ) -> dict[str, object]:
        return self._analysis.advance_activity(activity_id, as_of)

    def verify_site(self, name: str, as_of: datetime | None = None) -> dict[str, object]:
        return self._catalog.verify_site(name, as_of)

    async def poll_chat(self) -> list[GpsFix]:
        return await self._ingest.poll_chat()

    async def poll_tacrep(self) -> list[Report]:
        return await self._ingest.poll_tacrep()

    async def publish_if_configured(self) -> None:
        await self._publish.publish_if_configured()

    async def publish_routed(self) -> None:
        await self._publish.publish_routed()

    async def run_chat_loop(self) -> None:
        await self._loop.run_chat_loop()

    async def poll_report_layers(self) -> list[Report]:
        return await self._ingest.poll_report_layers()

    async def run_report_loop(self) -> None:
        await self._loop.run_report_loop()

    def publish_output(self) -> dict[str, object] | None:
        return self._publish.publish_output()

    def _refresh_correlations(self) -> None:
        self._state._refresh_correlations()

    def auth_policy(self) -> AuthPolicy:
        return self._state.auth_policy()

    async def run_analysis_loop(self) -> None:
        await self._loop.run_analysis_loop(self.analyze)
