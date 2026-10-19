"""Mutable analytics catalog shared by the focused application services."""

import asyncio
from dataclasses import dataclass, field, replace
from pathlib import Path

from analytics.application.chat_read import ChatReadHealth
from analytics.application.coordinates import load_override, override_file
from analytics.application.hub import OutputHub
from analytics.application.ports import (
    DocumentSink,
    HexIndex,
    ReferenceFiles,
    ReportSource,
    RoomPublisher,
    RouteEngine,
)
from analytics.application.room_memory import RoomMemory
from analytics.application.settings import AnalyticsSettings
from analytics.domain.access import AuthPolicy
from analytics.domain.aoi import AreaOfInterest
from analytics.domain.behavior import Mill, Slowdown, Stop
from analytics.domain.card_schema import SchemaCatalog, bundled_catalog
from analytics.domain.catce import DEFAULT_CAT_CE_METERS
from analytics.domain.confidence_text import DEFAULT_CONFIDENCE_TERMS, ConfidenceTerm
from analytics.domain.coordcard import CoordCard, MgrsGrid
from analytics.domain.coordinates import OutputCoordinates
from analytics.domain.correlation import (
    Activity,
    Correlation,
    CorrelationPolicy,
    TacrepBlock,
    TacrepMessage,
    correlate,
)
from analytics.domain.grid import CellActivity
from analytics.domain.reference import ArchiveLimits, Bmoa, Msr, Site
from analytics.domain.reports import Report
from analytics.domain.routing import RoutingError, VehicleProfile, default_profiles
from analytics.domain.templates import Tasking
from analytics.domain.tracks import GpsFix
from navtracker_common.chatsurfer import ChatSurferApiClient


@dataclass
class AnalyticsState:
    """One in-memory exercise. Services share this object and nothing else."""

    settings: AnalyticsSettings
    chat: ChatSurferApiClient | None
    report_source: ReportSource | None
    frontend: DocumentSink | None
    hub: OutputHub
    _references: ReferenceFiles
    _index: HexIndex
    _router: RouteEngine
    _mgrs: MgrsGrid
    _publisher: RoomPublisher | None
    _output: dict[str, object] | None = None
    _fixes: list[GpsFix] = field(default_factory=list)
    _slowdowns: list[Slowdown] = field(default_factory=list)
    _stops: list[Stop] = field(default_factory=list)
    _mills: list[Mill] = field(default_factory=list)
    _areas: list[AreaOfInterest] = field(default_factory=list)
    _imported: tuple[Site, ...] = ()
    _sites: tuple[Site, ...] = ()
    _msrs: tuple[Msr, ...] = ()
    _cells: tuple[CellActivity, ...] = ()
    _reports: tuple[Report, ...] = ()
    _messages: tuple[TacrepMessage, ...] = ()
    _correlations: tuple[Correlation, ...] = ()
    _activity_overrides: dict[str, Activity] = field(default_factory=dict)
    _bmoas: tuple[Bmoa, ...] = ()
    _msr_reports: dict[str, tuple[Report, ...]] = field(default_factory=dict)
    _cursors: dict[str, str] = field(default_factory=dict)
    _report_cursors: dict[str, str] = field(default_factory=dict)
    _published: str = ""
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _taskings: tuple[Tasking, ...] = ()
    _published_levels: dict[str, int] = field(default_factory=dict)
    _published_phrases: dict[str, str] = field(default_factory=dict)
    _output_override: OutputCoordinates | None = None
    _coord_card: CoordCard = field(default_factory=CoordCard)
    _coord_card_file: Path | None = None
    _schema_catalog: SchemaCatalog = field(default_factory=bundled_catalog)
    _card_document: dict[str, object] | None = None
    _card_schema_version: str = ""
    _schema_path: Path | None = None
    _chat_reads: ChatReadHealth = field(default_factory=ChatReadHealth)
    _room_memory: dict[str, RoomMemory] = field(default_factory=dict)
    _scrubbed_ids: set[str] = field(default_factory=set)
    _chat_transport: str = "poll"

    def _refresh_correlations(self) -> None:
        self._correlations = correlate(
            self._messages,
            self._policy(),
            neighbors=self._neighbor_cells,
        )
        live = {item.activity.activity_id for item in self._correlations}
        self._activity_overrides = {
            key: value for key, value in self._activity_overrides.items() if key in live
        }

    def _policy(self) -> CorrelationPolicy:
        blocks = tuple(
            TacrepBlock(
                kind=letter,
                start=item.start,
                end=item.end,
                tacrep_type=item.name,
            )
            for item in self._coord_card.types
            for letter in item.kinds
        )
        return replace(self.settings.correlation, blocks=blocks)

    def _archive_limits(self) -> ArchiveLimits:
        return ArchiveLimits(
            max_entries=self.settings.max_archive_entries,
            max_bytes=self.settings.max_archive_bytes,
        )

    def _neighbor_cells(self, cell_id: str, k: int) -> set[str]:
        return set(self._index.disk(cell_id, k))

    def _cat_ce(self) -> tuple[tuple[int, float], ...]:
        return self.settings.cat_ce or DEFAULT_CAT_CE_METERS

    def _terms(self) -> tuple[ConfidenceTerm, ...]:
        found = tuple(
            item for item in self.settings.confidence_terms if isinstance(item, ConfidenceTerm)
        )
        return found or DEFAULT_CONFIDENCE_TERMS

    def _profiles(self) -> tuple[VehicleProfile, ...]:
        if self.settings.routing_profiles:
            return self.settings.routing_profiles
        return default_profiles()

    def _named_site(self, name: str) -> Site:
        for site in self._sites:
            if site.name == name:
                return site
        raise RoutingError(f"Unknown site {name}.", "missing_site")

    def _named_profile(self, name: str) -> VehicleProfile:
        for profile in self._profiles():
            if profile.name == name:
                return profile
        raise RoutingError(f"Unknown vehicle profile {name}.")

    def auth_policy(self) -> AuthPolicy:
        origins = self.settings.ws_origins
        return AuthPolicy(
            read_token=self.settings.read_token,
            write_token=self.settings.write_token,
            origins=origins,
        )


def make_state(
    settings: AnalyticsSettings,
    chat: ChatSurferApiClient | None,
    report_source: ReportSource | None,
    frontend: DocumentSink | None,
    *,
    index: HexIndex,
    router: RouteEngine,
    references: ReferenceFiles,
    grid: MgrsGrid,
    hub: OutputHub | None = None,
    publisher: RoomPublisher | None = None,
) -> AnalyticsState:
    """Build the shared catalog and the default card. Does not read the card file."""

    state = AnalyticsState(
        settings=settings,
        chat=chat,
        report_source=report_source,
        frontend=frontend,
        hub=hub or OutputHub(),
        _references=references,
        _index=index,
        _router=router,
        _mgrs=grid,
        _publisher=publisher,
    )
    state._coord_card = CoordCard(
        mismatch_meters=settings.coord_mismatch_meters,
        ellipse_unit=settings.ellipse_unit,
        magnetic_declination_degrees=settings.magnetic_declination_degrees,
        cat_ce=state._cat_ce(),
        confidence_terms=state._terms(),
    )
    state._coord_card_file = _coord_card_file(settings.coord_card_path)
    state._schema_path = schema_destination(settings)
    state._output_override = load_override(override_file(settings))
    return state


def schema_destination(settings: AnalyticsSettings) -> Path:
    """Where an uploaded schema is stored. A configured path wins."""

    configured = settings.coord_card_schema_path.strip()
    if configured:
        return Path(configured)
    directory = Path(settings.reference_dir.strip() or "reference")
    return directory / "coord-card.schema.json"


def _coord_card_file(path: str) -> Path | None:
    text = path.strip()
    if not text:
        return None
    return Path(text)
