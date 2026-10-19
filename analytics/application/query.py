"""Read models: health, sites, snapshots, and activity documents."""

from datetime import UTC, datetime

from analytics.application.analysis import _activity_document
from analytics.application.dispatch import ingest_room_list
from analytics.application.events import event_document
from analytics.application.state import AnalyticsState
from analytics.domain.aoi import AreaOfInterest
from analytics.domain.correlation import (
    Correlation,
    advance_for_gap,
    linked_ids,
    session_labels,
)
from analytics.domain.grid import CellActivity
from analytics.domain.outputs import (
    basemap_document,
    build_snapshot,
    fusion_document,
    routing_metadata,
)
from analytics.domain.reference import (
    Msr,
    Site,
)
from analytics.domain.reports import (
    Report,
)


class QueryService:
    def __init__(self, state: AnalyticsState) -> None:
        self._state = state

    def health(self) -> dict[str, object]:
        state = self._state
        rooms: list[str] = []
        configured = (
            *state.settings.read_rooms,
            *ingest_room_list(state._coord_card, state.settings),
        )
        for room in configured:
            if room not in rooms:
                rooms.append(room)
        remaining: int | None = None
        if state.chat is not None:
            remaining = state.chat.rate_remaining
        mode = "disabled"
        if state.settings.chat_read_enabled:
            mode = state._chat_transport or state.settings.chat_ingest_mode
        return {
            "status": "ok",
            "fixes": len(state._fixes),
            "sites": len(state._sites),
            "msrs": len(state._msrs),
            "cells": len(state._cells),
            "ingest": {
                "chat": {
                    "mode": mode,
                    "rate_remaining": remaining,
                    "rooms": state._chat_reads.snapshot(
                        rooms,
                        enabled=state.settings.chat_read_enabled,
                    ),
                }
            },
        }

    def events(self) -> list[object]:
        state = self._state
        document = event_document(
            state._slowdowns, state._stops, state._mills, state._areas
        )
        events = document["events"]
        if isinstance(events, list):
            return events
        return []

    def areas(self) -> list[AreaOfInterest]:
        state = self._state
        return list(state._areas)

    def sites(self) -> list[Site]:
        state = self._state
        return list(state._sites)

    def routes(self) -> list[Msr]:
        state = self._state
        return list(state._msrs)

    def cells(self) -> list[CellActivity]:
        state = self._state
        return list(state._cells)

    def corridor_meters(self) -> float:
        state = self._state
        return state.settings.thresholds.msr_corridor_meters

    def msr_reports(self) -> dict[str, tuple[Report, ...]]:
        state = self._state
        return dict(state._msr_reports)

    def public_base(self) -> str:
        state = self._state
        return state.settings.public_base_url

    def basemap(self) -> dict[str, object]:
        state = self._state
        settings = state.settings
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
        state = self._state
        return routing_metadata(
            state.settings.routing_engine,
            state.settings.routing_base_url,
            state._profiles(),
        )

    def output_snapshot(self, as_of: datetime | None = None) -> dict[str, object]:
        state = self._state
        moment = as_of or datetime.now(UTC)
        return build_snapshot(
            as_of=moment,
            basemap=self.basemap(),
            routing=self.routing_info(),
            sites=state._sites,
            cells=state._cells,
            routes=state._msrs,
            route_reports=state._msr_reports,
            suggested_fused=tuple(
                fusion_document(item.suggested_fusion)
                for item in state._correlations
                if item.suggested_fusion is not None
            ),
        )

    def api_sites(self) -> list[dict[str, object]]:
        state = self._state
        return [_api_site(site) for site in state._sites]

    def correlations_document(self) -> dict[str, object]:
        state = self._state
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
                    (item, session_labels(item.messages)) for item in state._correlations
                )
            ]
        }

    def activities_document(self, as_of: datetime | None = None) -> dict[str, object]:
        state = self._state
        moment = as_of or datetime.now(UTC)
        return {"activities": [_activity_row(item, moment, state) for item in state._correlations]}


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
    state: AnalyticsState,
) -> dict[str, object]:
    current = state._activity_overrides.get(
        correlation.activity.activity_id,
        correlation.activity,
    )
    current = advance_for_gap(
        current,
        as_of,
        state.settings.correlation.hide_gap_minutes,
    )
    return _activity_document(current)

