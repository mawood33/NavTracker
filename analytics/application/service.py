"""In-process analytics state. ``run`` opens the network; construction does not."""

import asyncio
import json
import logging
from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol

from analytics.application.dispatch import (
    active_publish_table,
    destination_rooms,
    ingest_room_list,
    phrase_for_message,
)
from analytics.application.events import event_document
from analytics.application.hub import OutputHub
from analytics.application.ports import HexIndex, ReferenceFiles, RoomPublisher, RouteEngine
from analytics.application.settings import AnalyticsSettings
from analytics.domain.access import AuthPolicy
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
from analytics.domain.catce import DEFAULT_CAT_CE_METERS
from analytics.domain.confidence_text import DEFAULT_CONFIDENCE_TERMS, ConfidenceTerm
from analytics.domain.coordcard import CoordCard, MgrsGrid, parse_coord_card_text
from analytics.domain.correlation import (
    Activity,
    Correlation,
    CorrelationPolicy,
    TacrepBlock,
    TacrepMessage,
    advance_for_gap,
    correlate,
    dedupe_messages,
    linked_ids,
    manual_advance,
    message_from_event,
    session_labels,
    state_site_type,
)
from analytics.domain.grid import CellActivity, aggregate_cells
from analytics.domain.outputs import (
    basemap_document,
    build_snapshot,
    diff_documents,
    fusion_document,
    routing_metadata,
)
from analytics.domain.publish import (
    PHRASE_RANK,
    PublishNotice,
    PublishTable,
    describe_table,
    phrase_for_confidence,
    should_publish,
)
from analytics.domain.reference import (
    ArchiveLimits,
    Bmoa,
    Evidence,
    Msr,
    Site,
    match_bmoa,
    match_msr,
    named_bmoa,
    named_msr,
)
from analytics.domain.reports import (
    Report,
    dedupe_reports,
    parse_tacrep,
    select_route_reports,
)
from analytics.domain.routing import (
    METERS_PER_MILE,
    RoutingError,
    VehicleProfile,
    default_profiles,
    limit_speed,
    reject_unsupported_vehicle,
    route_document,
)
from analytics.domain.tacrep_parse import parse_exercise_text
from analytics.domain.templates import Tasking, link_responses
from analytics.domain.timeutil import ensure_utc, valid_position
from analytics.domain.tracks import GpsFix, dedupe_fixes, fix_from_mapping
from navtracker_common.chatsurfer import ChatSurferApiClient
from navtracker_common.errors import PublicationError

logger = logging.getLogger(__name__)


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
        *,
        index: HexIndex,
        router: RouteEngine,
        references: ReferenceFiles,
        grid: MgrsGrid,
        hub: OutputHub | None = None,
        publisher: RoomPublisher | None = None,
    ):
        self.settings = settings
        self.chat = chat
        self.report_source = report_source
        self.frontend = frontend
        self.hub = hub or OutputHub()
        self._references = references
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
        self._index = index
        self._router = router
        self._cursors: dict[str, str] = {}
        self._report_cursors: dict[str, str] = {}
        self._published = ""
        self._lock = asyncio.Lock()
        self._mgrs = grid
        self._publisher = publisher
        self._taskings: tuple[Tasking, ...] = ()
        self._published_levels: dict[str, int] = {}
        self._coord_card = CoordCard(
            mismatch_meters=settings.coord_mismatch_meters,
            ellipse_unit=settings.ellipse_unit,
            magnetic_declination_degrees=settings.magnetic_declination_degrees,
            cat_ce=self._cat_ce(),
            confidence_terms=self._terms(),
        )
        self._coord_card_file = _coord_card_file(settings.coord_card_path)
        self._load_coord_card_file()

    def load_reference(self) -> dict[str, int]:
        directory = self.settings.reference_dir.strip()
        if not directory:
            self._imported = ()
            self._sites = tuple(
                site for site in self._sites if site.origin == "discovered"
            )
            self._msrs = ()
            return {"sites": len(self._sites), "msrs": 0}
        loaded = self._references.load_dir(
            Path(directory),
            columns=self.settings.columns,
            limits=self._archive_limits(),
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
        name = Path(filename).name
        if not name or name != filename or name.startswith("."):
            raise ValueError("reference upload name must be a single file name")
        if len(body) > self.settings.max_upload_bytes:
            raise ValueError("upload exceeds max_upload_bytes")
        self._references.parse_bytes(
            name,
            body,
            columns=self.settings.columns,
            limits=self._archive_limits(),
        )
        directory = Path(self.settings.reference_dir.strip() or "reference")
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / name
        temporary = directory / f".{name}.upload"
        temporary.write_bytes(body)
        temporary.replace(target)
        return self.load_reference()

    def add_fixes(self, fixes: list[GpsFix]) -> None:
        if not fixes:
            return
        kept: list[GpsFix] = []
        for fix in fixes:
            if not valid_position(fix.latitude, fix.longitude):
                continue
            kept.append(replace(fix, timestamp=ensure_utc(fix.timestamp)))
        self._fixes.extend(kept)
        self._fixes = _retain_fixes(self._fixes, self.settings.fix_retention_seconds)

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
            (ensure_utc(fix.timestamp) for fix in deduped),
            default=datetime.now(UTC),
        )
        as_of = max(
            [as_of, *(ensure_utc(report.observed_at) for report in self._reports)],
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
        self._refresh_correlations()
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
            suggested_fused=tuple(
                fusion_document(item.suggested_fusion)
                for item in self._correlations
                if item.suggested_fusion is not None
            ),
        )

    def add_reports(self, reports: list[Report]) -> None:
        if not reports:
            return
        normalized = [
            replace(report, observed_at=ensure_utc(report.observed_at)) for report in reports
        ]
        combined = dedupe_reports((*self._reports, *normalized))
        self._reports = combined[-2000:]

    def add_messages(self, messages: list[TacrepMessage]) -> None:
        if not messages:
            return
        stamped: list[TacrepMessage] = []
        for message in messages:
            if not valid_position(message.latitude, message.longitude):
                continue
            cell_id = self._index.cell_id(message.latitude, message.longitude)
            bmoa = named_bmoa(message.bmoa, self._bmoas) if message.bmoa else ""
            if message.bmoa and bmoa not in {area.name for area in self._bmoas}:
                for site in self._sites:
                    if site.name.strip().upper() == message.bmoa.strip().upper():
                        bmoa = site.name
                        break
            if not bmoa:
                area = match_bmoa(message.latitude, message.longitude, self._bmoas)
                if area is not None:
                    bmoa = area.name
            msr_name = named_msr(message.msr, self._msrs) if message.msr else ""
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
                replace(
                    message,
                    observed_at=ensure_utc(message.observed_at),
                    cell_id=cell_id,
                    bmoa=bmoa,
                    msr=msr_name,
                    vacant_weight=self.settings.vacant_evidence_weight,
                )
            )
        combined = dedupe_messages((*self._messages, *stamped))[-2000:]
        self._messages = link_responses(
            combined,
            self._taskings,
            match_meters=self._coord_card.mismatch_meters,
        )
        self._refresh_correlations()

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
        source_id: str = "",
    ) -> dict[str, object]:
        messages = self._messages_from_payload(payload, source_id)
        if not messages:
            raise ValueError("unreadable event")
        self.add_messages(messages)
        return {"accepted": len(messages)}

    def ingest_tacrep_body(self, body: str, source_id: str) -> dict[str, object]:
        messages, taskings = self._read_text(body, source_id, None)
        if not messages and not taskings:
            raise ValueError("unreadable tacrep")
        if messages:
            self.add_messages(messages)
        return {"accepted": len(messages), "taskings": len(taskings)}

    def ingest_batch(
        self,
        payload: Mapping[str, object],
        source_id: str = "",
    ) -> dict[str, object]:
        raw = payload.get("events", payload.get("messages", []))
        if not isinstance(raw, list):
            raise ValueError("events must be a list")
        batch_source = str(payload.get("source_id") or source_id)
        found: list[TacrepMessage] = []
        for item in raw:
            if isinstance(item, str):
                messages, _taskings = self._read_text(item, batch_source, None)
                found.extend(messages)
            elif isinstance(item, dict):
                found.extend(self._messages_from_payload(item, batch_source))
        if found:
            self.add_messages(found)
        return {"accepted": len(found)}

    def save_coord_card(self, filename: str, body: bytes) -> dict[str, int]:
        """Validate, then replace the card. A bad card leaves the previous one."""

        if len(body) > self.settings.max_upload_bytes:
            raise ValueError("coord card exceeds the upload limit")
        name = filename.strip() or "coord-card.yaml"
        if "/" in name or "\\" in name or name.startswith("."):
            raise ValueError("coord card filename must be a single file name")
        if not name.lower().endswith((".yaml", ".yml", ".csv")):
            raise ValueError("coord card must be YAML or CSV")
        text = body.decode("utf-8")
        card = self._parse_card(text, name)
        target = self._coord_card_target(name)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.parent / f".{target.name}.upload"
        temporary.write_text(text, encoding="utf-8")
        temporary.replace(target)
        self._coord_card_file = target
        self._install_card(card)
        return {"types": len(card.types), "templates": len(card.templates)}

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
                    "tacrep_type": labels[0],
                    "block": labels[1],
                    "warning": labels[2],
                    "superseded": list(labels[3]),
                    "linked_ids": list(linked_ids(item.messages)),
                    "suggested_fused": None
                    if item.suggested_fusion is None
                    else fusion_document(item.suggested_fusion),
                }
                for item, labels in (
                    (item, session_labels(item.messages)) for item in self._correlations
                )
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
        rooms = ingest_room_list(self._coord_card, self.settings)
        if client is None or not rooms:
            return []
        found: list[Report] = []
        pending: list[TacrepMessage] = []
        for room in rooms:
            messages = await client.read_room(
                room, since=self._report_cursors.get(room)
            )
            for message in messages:
                if message.message_id:
                    self._report_cursors[room] = message.message_id
                parsed, _taskings = self._read_text(message.body, room, message.timestamp)
                if parsed:
                    pending.extend(parsed)
                    continue
                report = parse_tacrep(
                    message.body,
                    layer=room,
                    message_id=message.message_id,
                    received_at=message.timestamp,
                    policy=self.settings.report_policy,
                )
                if report is not None:
                    found.append(report)
        if pending:
            self.add_messages(pending)
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

    async def publish_routed(self) -> None:
        """Publish confidence and CAT notices. No rooms means no publish."""

        publisher = self._publisher
        if publisher is None:
            return
        table = active_publish_table(self._coord_card, self.settings)
        high_at = _high_at(self._terms())
        for notice, rooms in self._notices(table, high_at):
            if not rooms:
                continue
            if not should_publish(
                self._published_levels.get(notice.identity),
                notice.level_rank,
                republish_on_higher_level=table.republish_on_higher_level,
            ):
                continue
            logger.info("Publishing %s to %s", notice.identity, list(rooms))
            for room in rooms:
                await publisher.publish_to(room, notice.body)
            self._published_levels[notice.identity] = notice.level_rank

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

    def _messages_from_payload(
        self,
        payload: Mapping[str, object],
        source_id: str,
    ) -> list[TacrepMessage]:
        source = str(payload.get("source_id") or source_id)
        wrapped = payload.get("tacrep", payload.get("message", payload.get("body")))
        if isinstance(wrapped, str):
            messages, taskings = self._read_text(wrapped, source, None)
            if messages or taskings:
                return messages
        message = message_from_event(payload, source_id=source, policy=self._policy())
        if message is None:
            return []
        return [message]

    def _coord_card_target(self, filename: str) -> Path:
        configured = self.settings.coord_card_path.strip()
        if configured:
            return Path(configured)
        directory = Path(self.settings.reference_dir.strip() or "reference")
        return directory / filename

    def _load_coord_card_file(self) -> None:
        path = self._coord_card_file
        if path is None or not path.is_file():
            return
        try:
            self._install_card(self._parse_card(path.read_text(encoding="utf-8"), path.name))
        except ValueError:
            logger.exception("Coord card at %s was not loaded", path)

    def _read_text(
        self,
        body: str,
        source_id: str,
        observed_at: datetime | None,
    ) -> tuple[list[TacrepMessage], tuple[Tasking, ...]]:
        messages, taskings = parse_exercise_text(
            body,
            source_id=source_id,
            observed_at=observed_at,
            card=self._coord_card,
            grid=self._mgrs,
            policy=self._policy(),
        )
        if taskings:
            self._taskings = (*self._taskings, *taskings)
            if self._messages:
                self._messages = link_responses(
                    self._messages,
                    self._taskings,
                    match_meters=self._coord_card.mismatch_meters,
                )
                self._refresh_correlations()
        return list(messages), taskings

    def _parse_card(self, text: str, name: str) -> CoordCard:
        return parse_coord_card_text(
            text,
            suffix=name,
            mismatch_meters=self.settings.coord_mismatch_meters,
            ellipse_unit=self.settings.ellipse_unit,
            magnetic_declination_degrees=self.settings.magnetic_declination_degrees,
            cat_ce=self._cat_ce(),
            confidence_terms=self._terms(),
        )

    def _install_card(self, card: CoordCard) -> None:
        previous = ingest_room_list(self._coord_card, self.settings)
        previous_routes = describe_table(active_publish_table(self._coord_card, self.settings))
        self._coord_card = card
        current = ingest_room_list(card, self.settings)
        if previous != current:
            logger.info(
                "ChatSurfer ingest rooms changed from %s to %s",
                list(previous),
                list(current),
            )
        routes = describe_table(active_publish_table(card, self.settings))
        if routes != previous_routes:
            logger.info("ChatSurfer publish routing %s", routes)

    def _cat_ce(self) -> tuple[tuple[int, float], ...]:
        return self.settings.cat_ce or DEFAULT_CAT_CE_METERS

    def _terms(self) -> tuple[ConfidenceTerm, ...]:
        found = tuple(
            item for item in self.settings.confidence_terms if isinstance(item, ConfidenceTerm)
        )
        return found or DEFAULT_CONFIDENCE_TERMS

    def _notices(
        self,
        table: PublishTable,
        high_at: float,
    ) -> list[tuple[PublishNotice, tuple[str, ...]]]:
        probable = self.settings.confidence.probable_at
        confirmed = self.settings.confidence.confirmed_at
        found: list[tuple[PublishNotice, tuple[str, ...]]] = []
        for message in self._messages:
            phrase = phrase_for_message(
                message,
                probable_at=probable,
                confirmed_at=confirmed,
                high_at=high_at,
            )
            identity = message.serial_full or message.source_message_id
            notice = PublishNotice(
                identity=identity,
                confidence=phrase,
                cat=message.cat_level,
                types=(message.tacrep_type,) if message.tacrep_type else (),
                sources=(message.source_id,) if message.source_id else (),
                body=message.raw or identity,
                level_rank=PHRASE_RANK.get(phrase, 0),
            )
            rooms = destination_rooms(
                message.tacrep_type,
                confidence=phrase,
                cat=message.cat_level,
                sources=notice.sources,
                report_room=message.report_room,
                card=self._coord_card,
                settings=self.settings,
                table=table,
            )
            found.append((notice, rooms))
        for site in self._sites:
            phrase = phrase_for_confidence(site.confidence, probable, confirmed, high_at)
            notice = PublishNotice(
                identity=f"site:{site.name}",
                confidence=phrase,
                cat=None,
                types=tuple(sorted(site.site_types)),
                sources=("site",),
                body=f"site {site.name} {phrase}",
                level_rank=PHRASE_RANK.get(phrase, 0),
            )
            rooms = destination_rooms(
                "",
                confidence=phrase,
                cat=None,
                sources=notice.sources,
                report_room="",
                card=self._coord_card,
                settings=self.settings,
                table=table,
            )
            found.append((notice, rooms))
        return found

    def auth_policy(self) -> AuthPolicy:
        origins = self.settings.ws_origins
        return AuthPolicy(
            read_token=self.settings.read_token,
            write_token=self.settings.write_token,
            origins=origins,
        )

    async def run_analysis_loop(self) -> None:
        while True:
            try:
                async with self._lock:
                    await asyncio.to_thread(self.analyze)
                    await self.publish_if_configured()
                    await self.publish_routed()
                    message = self.publish_output()
                    if message is not None and self.frontend is not None:
                        try:
                            await self.frontend.publish(message)
                        except Exception:
                            logger.exception("Frontend publication failed")
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Analysis failed")
            await asyncio.sleep(self.settings.analysis_interval_seconds)


def _high_at(terms: tuple[ConfidenceTerm, ...]) -> float:
    for term in terms:
        if term.phrase == "POSS HIGH":
            return term.weight
    return 0.45


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


def _coord_card_file(path: str) -> Path | None:
    text = path.strip()
    if not text:
        return None
    return Path(text)


def _retain_fixes(fixes: list[GpsFix], seconds: float) -> list[GpsFix]:
    """Drop fixes older than ``seconds`` before each vehicle's newest fix."""

    if seconds <= 0 or not fixes:
        return fixes
    newest: dict[str, datetime] = {}
    for fix in fixes:
        current = newest.get(fix.vehicle_id)
        if current is None or fix.timestamp > current:
            newest[fix.vehicle_id] = fix.timestamp
    kept: list[GpsFix] = []
    for fix in fixes:
        cutoff = newest[fix.vehicle_id] - timedelta(seconds=seconds)
        if fix.timestamp >= cutoff:
            kept.append(fix)
    return kept


def _json_object(body: str) -> dict[str, object] | None:
    try:
        payload = json.loads(body)
    except ValueError:
        return None
    if isinstance(payload, dict):
        return payload
    return None
