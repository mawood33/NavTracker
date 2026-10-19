"""In-process analytics state. ``run`` opens the network; construction does not."""

import asyncio
import json
import logging
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from analytics.domain.aoi import AreaOfInterest, derive_areas
from analytics.domain.assessment import merge_catalog, stamp_import, update_sites
from analytics.domain.behavior import (
    Mill,
    Slowdown,
    Stop,
    detect_milling,
    detect_slowdowns,
    detect_stops,
)
from analytics.domain.grid import CellActivity, aggregate_cells
from analytics.domain.outputs import (
    basemap_document,
    build_snapshot,
    diff_documents,
    routing_metadata,
)
from analytics.domain.reference import Msr, Site
from analytics.domain.reports import (
    Report,
    dedupe_reports,
    parse_tacrep,
    select_route_reports,
)
from analytics.domain.routing import (
    RouteCache,
    RoutingError,
    VehicleProfile,
    default_profiles,
    route_document,
)
from analytics.domain.tracks import GpsFix, dedupe_fixes, fix_from_mapping
from analytics.infrastructure.config import AnalyticsSettings
from analytics.infrastructure.h3_index import H3Index
from analytics.infrastructure.http_api import event_document
from analytics.infrastructure.reference_parse import load_reference_dir, load_reference_file
from analytics.infrastructure.routing_engines import build_router
from analytics.infrastructure.websocket import OutputHub
from navtracker_common.chatsurfer import ChatSurferApiClient
from navtracker_common.errors import PublicationError

logger = logging.getLogger(__name__)

_MAX_FIXES = 5000


class ReportSource(Protocol):
    async def poll(self) -> list[Report]:
        """Return newly observed reports. Does not have to be called at import."""


class DocumentSink(Protocol):
    async def publish(self, document: Mapping[str, object]) -> None:
        """Deliver one snapshot or diff. Called only after analysis."""


class AnalyticsService:
    def __init__(
        self,
        settings: AnalyticsSettings,
        chat: ChatSurferApiClient | None,
        report_source: ReportSource | None = None,
        frontend: DocumentSink | None = None,
    ):
        self.settings = settings
        self.chat = chat
        self.report_source = report_source
        self.frontend = frontend
        self.hub = OutputHub()
        self._output: dict[str, object] | None = None
        self._fixes: list[GpsFix] = []
        self._slowdowns: list[Slowdown] = []
        self._stops: list[Stop] = []
        self._mills: list[Mill] = []
        self._areas: list[AreaOfInterest] = []
        self._imported: tuple[Site, ...] = ()
        self._sites: tuple[Site, ...] = ()
        self._msrs: tuple[Msr, ...] = ()
        self._cells: tuple[CellActivity, ...] = ()
        self._reports: tuple[Report, ...] = ()
        self._msr_reports: dict[str, tuple[Report, ...]] = {}
        self._index = H3Index(settings.grid.resolution)
        self._router = build_router(
            settings.routing_engine,
            settings.routing_base_url,
            RouteCache(
                settings.routing_cache_size,
                settings.routing_cache_ttl_seconds,
            ),
            settings.routing_snap_meters,
            settings.routing_join_meters,
        )
        self._cursors: dict[str, str] = {}
        self._report_cursors: dict[str, str] = {}
        self._published = ""
        self._lock = asyncio.Lock()

    def load_reference(self) -> dict[str, int]:
        directory = self.settings.reference_dir.strip()
        if not directory:
            self._imported = ()
            self._sites = tuple(
                site for site in self._sites if site.origin == "discovered"
            )
            self._msrs = ()
            return {"sites": len(self._sites), "msrs": 0}
        loaded = load_reference_dir(
            Path(directory),
            columns=self.settings.columns,
        )
        self._replace_catalog(loaded.sites, loaded.msrs)
        logger.info(
            "Loaded reference sites=%s msrs=%s from %s",
            len(self._sites),
            len(self._msrs),
            directory,
        )
        return {"sites": len(self._sites), "msrs": len(self._msrs)}

    def reload_reference(self) -> dict[str, int]:
        return self.load_reference()

    def save_upload(self, filename: str, body: bytes) -> dict[str, int]:
        directory = Path(self.settings.reference_dir.strip() or "reference")
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / filename
        target.write_bytes(body)
        loaded = load_reference_file(target, columns=self.settings.columns)
        self._replace_catalog(self._imported + loaded.sites, self._msrs + loaded.msrs)
        return {"sites": len(self._sites), "msrs": len(self._msrs)}

    def add_fixes(self, fixes: list[GpsFix]) -> None:
        if not fixes:
            return
        self._fixes.extend(fixes)
        if len(self._fixes) > _MAX_FIXES:
            self._fixes = self._fixes[-_MAX_FIXES:]

    def analyze(self) -> None:
        deduped = dedupe_fixes(
            self._fixes,
            self.settings.thresholds.dedupe_tolerance_seconds,
        )
        self._slowdowns = detect_slowdowns(deduped, self.settings.thresholds)
        self._stops = detect_stops(deduped, self.settings.thresholds)
        self._mills = detect_milling(self._stops, self.settings.thresholds)
        self._areas = derive_areas(
            self._stops,
            self._sites,
            self._msrs,
            self.settings.thresholds,
        )
        as_of = max(
            (fix.timestamp for fix in deduped),
            default=datetime.now(UTC),
        )
        as_of = max(
            [as_of, *(report.observed_at for report in self._reports)],
        )
        self._cells = aggregate_cells(
            self._stops,
            self._mills,
            self._msrs,
            as_of,
            self.settings.grid,
            self.settings.thresholds,
            self._index,
            self._reports,
            self.settings.report_policy,
        )
        self._sites = update_sites(
            self._sites,
            self._areas,
            self._stops,
            self._mills,
            self._msrs,
            as_of,
            self.settings.confidence,
            self.settings.thresholds,
            self._cells,
            self._index,
            self._reports,
            self.settings.report_policy,
        )
        self._msr_reports = {
            route.name: select_route_reports(
                route,
                self._reports,
                as_of,
                self.settings.report_policy,
                self.settings.thresholds,
            )
            for route in self._msrs
        }

    def health(self) -> dict[str, object]:
        return {
            "status": "ok",
            "fixes": len(self._fixes),
            "sites": len(self._sites),
            "msrs": len(self._msrs),
            "cells": len(self._cells),
        }

    def events(self) -> list[object]:
        document = event_document(
            self._slowdowns, self._stops, self._mills, self._areas
        )
        events = document["events"]
        if isinstance(events, list):
            return events
        return []

    def areas(self) -> list[AreaOfInterest]:
        return list(self._areas)

    def sites(self) -> list[Site]:
        return list(self._sites)

    def routes(self) -> list[Msr]:
        return list(self._msrs)

    def cells(self) -> list[CellActivity]:
        return list(self._cells)

    def corridor_meters(self) -> float:
        return self.settings.thresholds.msr_corridor_meters

    def msr_reports(self) -> dict[str, tuple[Report, ...]]:
        return dict(self._msr_reports)

    def public_base(self) -> str:
        return self.settings.public_base_url

    def basemap(self) -> dict[str, object]:
        settings = self.settings
        return basemap_document(
            settings.map_provider,
            settings.map_tile_url,
            settings.map_attribution,
            settings.map_min_zoom,
            settings.map_max_zoom,
        )

    def routing_info(self) -> dict[str, object]:
        return routing_metadata(
            self.settings.routing_engine,
            self.settings.routing_base_url,
            self._profiles(),
        )

    def route_between(
        self,
        origin_name: str,
        destination_name: str,
        profile_name: str,
    ) -> dict[str, object]:
        origin = self._named_site(origin_name)
        destination = self._named_site(destination_name)
        profile = self._named_profile(profile_name)
        if origin.latitude is None or origin.longitude is None:
            raise RoutingError(f"Site {origin.name} has no point.")
        if destination.latitude is None or destination.longitude is None:
            raise RoutingError(f"Site {destination.name} has no point.")
        if origin.name == destination.name:
            raise RoutingError("Origin and destination are the same site.")
        leg = self._router.route(
            (origin.latitude, origin.longitude),
            (destination.latitude, destination.longitude),
            profile,
            self._msrs,
        )
        return route_document(leg, origin.name, destination.name)

    def output_snapshot(self, as_of: datetime | None = None) -> dict[str, object]:
        moment = as_of or datetime.now(UTC)
        return build_snapshot(
            as_of=moment,
            basemap=self.basemap(),
            routing=self.routing_info(),
            sites=self._sites,
            cells=self._cells,
            routes=self._msrs,
            route_reports=self._msr_reports,
        )

    def add_reports(self, reports: list[Report]) -> None:
        if not reports:
            return
        combined = dedupe_reports((*self._reports, *reports))
        self._reports = combined[-2000:]

    def _profiles(self) -> tuple[VehicleProfile, ...]:
        if self.settings.routing_profiles:
            return self.settings.routing_profiles
        return default_profiles()

    def _named_site(self, name: str) -> Site:
        for site in self._sites:
            if site.name == name:
                return site
        raise RoutingError(f"Unknown site {name}.")

    def _named_profile(self, name: str) -> VehicleProfile:
        for profile in self._profiles():
            if profile.name == name:
                return profile
        raise RoutingError(f"Unknown vehicle profile {name}.")

    def _replace_catalog(
        self,
        imported: tuple[Site, ...],
        msrs: tuple[Msr, ...],
    ) -> None:
        stamped = tuple(
            site if site.origin == "import" else stamp_import(site, self.settings.confidence)
            for site in imported
        )
        self._imported = stamped
        self._msrs = msrs
        self._sites = merge_catalog(stamped, self._sites)

    async def poll_chat(self) -> list[GpsFix]:
        client = self.chat
        if client is None or not self.settings.read_rooms:
            return []
        found: list[GpsFix] = []
        for room in self.settings.read_rooms:
            messages = await client.read_room(room, since=self._cursors.get(room))
            for message in messages:
                if message.message_id:
                    self._cursors[room] = message.message_id
                payload = _json_object(message.body)
                if payload is None:
                    continue
                fix = fix_from_mapping(
                    payload,
                    source=f"chatsurfer:{room}",
                    speed_unit=self.settings.chat_speed_unit,
                )
                if fix is not None:
                    found.append(fix)
        return found

    async def poll_tacrep(self) -> list[Report]:
        client = self.chat
        if client is None or not self.settings.tacrep_rooms:
            return []
        found: list[Report] = []
        for room in self.settings.tacrep_rooms:
            messages = await client.read_room(
                room, since=self._report_cursors.get(room)
            )
            for message in messages:
                if message.message_id:
                    self._report_cursors[room] = message.message_id
                report = parse_tacrep(
                    message.body,
                    layer=room,
                    message_id=message.message_id,
                    received_at=message.timestamp,
                    policy=self.settings.report_policy,
                )
                if report is not None:
                    found.append(report)
        return found

    async def publish_if_configured(self) -> None:
        client = self.chat
        if client is None or not self.settings.publish_events:
            return
        document = event_document(
            self._slowdowns, self._stops, self._mills, self._areas
        )
        text = json.dumps(document, separators=(",", ":"))
        if text == self._published:
            return
        try:
            await client.publish_text(text)
        except PublicationError:
            logger.exception("Analytics publication failed")
            return
        self._published = text

    async def run_chat_loop(self) -> None:
        while True:
            try:
                self.add_fixes(await self.poll_chat())
                self.add_reports(await self.poll_tacrep())
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("ChatSurfer room read failed")
            await asyncio.sleep(self.settings.chat_poll_seconds)

    async def poll_report_layers(self) -> list[Report]:
        source = self.report_source
        if source is None:
            return []
        return await source.poll()

    async def run_report_loop(self) -> None:
        if self.report_source is None:
            return
        while True:
            try:
                self.add_reports(await self.poll_report_layers())
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Clairvoyant report layer read failed")
            await asyncio.sleep(self.settings.chat_poll_seconds)

    def publish_output(self) -> dict[str, object] | None:
        """Store a snapshot and broadcast it, or a later diff.

        The first call broadcasts the snapshot. A later call broadcasts
        only when sites, cells, or corridor reports changed.
        """

        current = self.output_snapshot()
        previous = self._output
        self._output = current
        self.hub.set_snapshot(current)
        if previous is None:
            self.hub.broadcast(current)
            return current
        update = diff_documents(previous, current)
        if update is None:
            return None
        self.hub.broadcast(update)
        return update

    async def run_analysis_loop(self) -> None:
        while True:
            async with self._lock:
                self.analyze()
                await self.publish_if_configured()
                message = self.publish_output()
                if message is not None and self.frontend is not None:
                    try:
                        await self.frontend.publish(message)
                    except Exception:
                        logger.exception("Frontend publication failed")
            await asyncio.sleep(self.settings.analysis_interval_seconds)


def _json_object(body: str) -> dict[str, object] | None:
    try:
        payload = json.loads(body)
    except ValueError:
        return None
    if isinstance(payload, dict):
        return payload
    return None
