"""In-process analytics state. ``run`` opens the network; construction does not."""

import asyncio
import json
import logging
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from analytics.domain.aoi import AreaOfInterest, derive_areas
from analytics.domain.assessment import (
    SiteActivity,
    assess_site,
    merge_catalog,
    stamp_import,
    update_sites,
)
from analytics.domain.behavior import (
    Mill,
    Slowdown,
    Stop,
    detect_milling,
    detect_slowdowns,
    detect_stops,
)
from analytics.domain.correlation import (
    Activity,
    Correlation,
    TacrepMessage,
    advance_for_gap,
    correlate,
    dedupe_messages,
    manual_advance,
    message_from_event,
    parse_tacrep_line,
    state_site_type,
)
from analytics.domain.grid import CellActivity, aggregate_cells
from analytics.domain.outputs import (
    basemap_document,
    build_snapshot,
    diff_documents,
    routing_metadata,
)
from analytics.domain.reference import Bmoa, Evidence, Msr, Site, match_bmoa, match_msr
from analytics.domain.reports import (
    Report,
    dedupe_reports,
    parse_tacrep,
    select_route_reports,
)
from analytics.domain.routing import (
    METERS_PER_MILE,
    RouteCache,
    RoutingError,
    VehicleProfile,
    default_profiles,
    limit_speed,
    reject_unsupported_vehicle,
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
        self._messages: tuple[TacrepMessage, ...] = ()
        self._correlations: tuple[Correlation, ...] = ()
        self._activity_overrides: dict[str, Activity] = {}
        self._bmoas: tuple[Bmoa, ...] = ()
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
            settings.routing_mapbox_token,
            settings.routing_timeout_seconds,
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
        self._bmoas = loaded.bmoas
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
        self._correlations = correlate(self._messages, self.settings.correlation)
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
            self._correlations,
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
            mapbox_token=settings.map_mapbox_token,
            center_latitude=settings.map_center_latitude,
            center_longitude=settings.map_center_longitude,
            zoom=settings.map_zoom,
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
        reject_unsupported_vehicle(profile_name)
        profile = limit_speed(self._named_profile(profile_name), None)
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

    def add_messages(self, messages: list[TacrepMessage]) -> None:
        if not messages:
            return
        stamped: list[TacrepMessage] = []
        for message in messages:
            cell_id = self._index.cell_id(message.latitude, message.longitude)
            bmoa = message.bmoa
            if not bmoa:
                area = match_bmoa(message.latitude, message.longitude, self._bmoas)
                if area is not None:
                    bmoa = area.name
            msr_name = message.msr
            if not msr_name:
                route = match_msr(
                    message.latitude,
                    message.longitude,
                    self._msrs,
                    self.settings.thresholds.msr_corridor_meters,
                )
                if route is not None:
                    msr_name = route.name
            stamped.append(
                TacrepMessage(
                    kind=message.kind,
                    sequence=message.sequence,
                    observed_at=message.observed_at,
                    latitude=message.latitude,
                    longitude=message.longitude,
                    source_id=message.source_id,
                    source_message_id=message.source_message_id,
                    subject=message.subject,
                    speed_mps=message.speed_mps,
                    heading=message.heading,
                    length_m=message.length_m,
                    width_m=message.width_m,
                    reported_confidence=message.reported_confidence,
                    bmoa=bmoa,
                    msr=msr_name,
                    cell_id=cell_id,
                    raw=message.raw,
                )
            )
        self._messages = dedupe_messages((*self._messages, *stamped))[-2000:]
        self._correlations = correlate(self._messages, self.settings.correlation)

    def api_sites(self) -> list[dict[str, object]]:
        return [_api_site(site) for site in self._sites]

    def api_route(
        self,
        origin_id: str,
        destination_id: str,
        vehicle_type: str,
        max_speed_mph: float | None,
    ) -> dict[str, object]:
        reject_unsupported_vehicle(vehicle_type)
        profile = limit_speed(self._named_profile(vehicle_type), max_speed_mph)
        origin = self._named_site(origin_id)
        destination = self._named_site(destination_id)
        if origin.latitude is None or origin.longitude is None:
            raise RoutingError(f"Site {origin.name} has no point.", "missing_point")
        if destination.latitude is None or destination.longitude is None:
            raise RoutingError(
                f"Site {destination.name} has no point.",
                "missing_point",
            )
        if origin.name == destination.name:
            raise RoutingError(
                "Origin and destination are the same site.",
                "same_site",
            )
        leg = self._router.route(
            (origin.latitude, origin.longitude),
            (destination.latitude, destination.longitude),
            profile,
            self._msrs,
        )
        return {
            "provider": leg.engine,
            "routes": [
                {
                    "originSiteId": origin.name,
                    "destinationSiteId": destination.name,
                    "vehicleType": vehicle_type,
                    "provider": leg.engine,
                    "distanceMeters": leg.distance_meters,
                    "distanceMiles": leg.distance_meters / METERS_PER_MILE,
                    "durationMinutes": leg.duration_seconds / 60,
                    "geometry": route_document(leg, origin.name, destination.name)["geometry"],
                }
            ],
        }

    def ingest_event(
        self,
        payload: Mapping[str, object],
        source_id: str = "event",
    ) -> dict[str, object]:
        message = message_from_event(payload, source_id=source_id)
        if message is None:
            raise ValueError("unreadable event")
        self.add_messages([message])
        return {"accepted": 1}

    def ingest_tacrep_body(self, body: str, source_id: str) -> dict[str, object]:
        message = parse_tacrep_line(body, source_id=source_id)
        if message is None:
            raise ValueError("unreadable tacrep")
        self.add_messages([message])
        return {"accepted": 1}

    def ingest_batch(self, payload: Mapping[str, object]) -> dict[str, object]:
        raw = payload.get("events", payload.get("messages", []))
        if not isinstance(raw, list):
            raise ValueError("events must be a list")
        accepted = 0
        for item in raw:
            if isinstance(item, str):
                message = parse_tacrep_line(item, source_id="batch")
            elif isinstance(item, dict):
                message = message_from_event(item, source_id="batch")
            else:
                message = None
            if message is None:
                continue
            self.add_messages([message])
            accepted += 1
        return {"accepted": accepted}

    def correlations_document(self) -> dict[str, object]:
        return {
            "correlations": [
                {
                    "cell_id": item.cell_id,
                    "window_start": item.window_start.isoformat(),
                    "label": item.label,
                    "best_score": item.best_score,
                    "source_ids": list(item.source_ids),
                    "site_types": sorted(item.site_types),
                    "activity_id": item.activity.activity_id,
                }
                for item in self._correlations
            ]
        }

    def activities_document(self, as_of: datetime | None = None) -> dict[str, object]:
        moment = as_of or datetime.now(UTC)
        return {"activities": [_activity_row(item, moment, self) for item in self._correlations]}

    def advance_activity(
        self,
        activity_id: str,
        as_of: datetime | None = None,
    ) -> dict[str, object]:
        moment = as_of or datetime.now(UTC)
        current = self._activity_by_id(activity_id)
        if current is None:
            raise KeyError(activity_id)
        advanced = manual_advance(current, moment)
        self._activity_overrides[activity_id] = advanced
        return _activity_document(advanced)

    def verify_site(self, name: str, as_of: datetime | None = None) -> dict[str, object]:
        moment = as_of or datetime.now(UTC)
        updated: list[Site] = []
        found = False
        for site in self._sites:
            if site.name != name:
                updated.append(site)
                continue
            found = True
            evidence = site.evidence
            if not any(item.kind == "truth" for item in evidence):
                evidence = evidence + (
                    Evidence(
                        kind="truth",
                        weight=self.settings.confidence.confirmed_at,
                        detail="manual verification",
                        observed_at=moment,
                    ),
                )
            stamped = Site(
                name=site.name,
                site_types=site.site_types,
                status=site.status,
                confidence=site.confidence,
                evidence=evidence,
                origin=site.origin,
                latitude=site.latitude,
                longitude=site.longitude,
                polygon=site.polygon,
                moa=site.moa,
                complex_name=site.complex_name,
                mgrs=site.mgrs,
                activity_status=site.activity_status,
                verified=True,
            )
            updated.append(
                assess_site(stamped, _quiet_activity(moment), self.settings.confidence)
            )
        if not found:
            raise KeyError(name)
        self._sites = tuple(updated)
        return _api_site(self._named_site(name))

    def _activity_by_id(self, activity_id: str) -> Activity | None:
        if activity_id in self._activity_overrides:
            return self._activity_overrides[activity_id]
        for item in self._correlations:
            if item.activity.activity_id == activity_id:
                return item.activity
        return None

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
                parsed = parse_tacrep_line(
                    message.body,
                    source_id=room,
                    fallback_message_id=message.message_id,
                )
                if parsed is not None:
                    self.add_messages([parsed])
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


def _quiet_activity(as_of: datetime) -> SiteActivity:
    return SiteActivity(
        as_of=as_of,
        stops=0,
        dwell_seconds=0.0,
        distinct_vehicles=0,
        revisits=0,
        milling=False,
        on_msr=False,
        long_dwell=False,
        cell_id="",
        cell_score=0.0,
        cell_hotspot=False,
        reports=(),
        last_activity_at=None,
    )


def _api_site(site: Site) -> dict[str, object]:
    assessment = "verified" if site.status == "confirmed" else site.status
    return {
        "id": site.name,
        "name": site.name,
        "latitude": site.latitude,
        "longitude": site.longitude,
        "status": site.activity_status,
        "assessment": assessment,
        "confidence": site.confidence,
        "types": sorted(site.site_types),
        "bmoa": site.moa,
    }


def _activity_row(
    correlation: Correlation,
    as_of: datetime,
    service: AnalyticsService,
) -> dict[str, object]:
    current = service._activity_overrides.get(
        correlation.activity.activity_id,
        correlation.activity,
    )
    current = advance_for_gap(
        current,
        as_of,
        service.settings.correlation.hide_gap_minutes,
    )
    return _activity_document(current)


def _activity_document(activity: Activity) -> dict[str, object]:
    return {
        "id": activity.activity_id,
        "state": activity.state,
        "cell_id": activity.cell_id,
        "bmoa": activity.bmoa,
        "latitude": activity.latitude,
        "longitude": activity.longitude,
        "acceptance": activity.acceptance,
        "updated_at": activity.updated_at.isoformat(),
        "site_type": state_site_type(activity.state),
        "source_ids": list(activity.source_ids),
    }


def _json_object(body: str) -> dict[str, object] | None:
    try:
        payload = json.loads(body)
    except ValueError:
        return None
    if isinstance(payload, dict):
        return payload
    return None
