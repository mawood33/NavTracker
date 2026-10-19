"""Versioned output documents for WebSocket clients and the OGC landing page.

Basemap and routing metadata are advertised here. ``leaflet`` is the
default and uses public OpenStreetMap tiles with no API key. ``mapbox``
selects the ``dark-v11`` style. Only ``MAPBOX_PUBLIC_TOKEN`` (or
``NEXT_PUBLIC_MAPBOX_TOKEN``) may appear in a tile URL. The server
routing token is not sent to clients. ``none`` is the air-gapped extra
and ``xyz`` repeats an operator URL. The default view is the contiguous
United States at zoom 4.
"""

from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import UTC, datetime

from analytics.domain.correlation import SuggestedFusion
from analytics.domain.grid import CellActivity
from analytics.domain.level_assessment import (
    LevelCatalog,
    LevelView,
    level_document,
    normalize_level,
)
from analytics.domain.reference import Evidence, Msr, Site
from analytics.domain.reports import Report, report_document
from analytics.domain.routing import VehicleProfile
from analytics.domain.tracks import GpsFix, dedupe_fixes

SCHEMA_NAME = "navtracker.analytics"
SCHEMA_VERSION = 2
OSM_TILE_URL = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
OSM_ATTRIBUTION = "© OpenStreetMap contributors"
MAPBOX_STYLE = "mapbox/dark-v11"
DEFAULT_VIEW_LATITUDE = 39.8283
DEFAULT_VIEW_LONGITUDE = -98.5795
DEFAULT_VIEW_ZOOM = 4


def basemap_document(
    provider: str,
    tile_url: str,
    attribution: str,
    min_zoom: int,
    max_zoom: int,
    *,
    mapbox_token: str = "",
    center_latitude: float = DEFAULT_VIEW_LATITUDE,
    center_longitude: float = DEFAULT_VIEW_LONGITUDE,
    zoom: int = DEFAULT_VIEW_ZOOM,
) -> dict[str, object]:
    view = (center_latitude, center_longitude, zoom)
    if provider in {"leaflet", "osm"}:
        return _basemap(
            "leaflet",
            OSM_TILE_URL,
            attribution or OSM_ATTRIBUTION,
            min_zoom,
            max_zoom,
            False,
            view,
            "",
        )
    if provider == "mapbox":
        return _basemap(
            "mapbox",
            _mapbox_tiles(mapbox_token),
            attribution or "© Mapbox © OpenStreetMap",
            min_zoom,
            max_zoom,
            False,
            view,
            mapbox_token,
        )
    if provider == "xyz":
        return _basemap(
            "xyz",
            tile_url,
            attribution,
            min_zoom,
            max_zoom,
            _private_tiles(tile_url),
            view,
            "",
        )
    return _basemap("none", "", attribution, min_zoom, max_zoom, True, view, "")


def routing_metadata(
    engine: str,
    base_url: str,
    profiles: Sequence[VehicleProfile],
) -> dict[str, object]:
    return {
        "engine": engine,
        "base_url": base_url,
        "fallback": "msr",
        "profiles": [
            {
                "name": profile.name,
                "max_speed_mps": profile.max_speed_mps,
                "weight_class": profile.weight_class,
                "vehicle_type": profile.vehicle_type,
                "eta_factor": profile.eta_factor,
            }
            for profile in profiles
        ],
    }


def fusion_document(fusion: SuggestedFusion) -> dict[str, object]:
    """A suggested FUSED correlation. ``sent`` is false because nobody transmitted it."""

    return {
        "anchor_id": fusion.anchor_id,
        "linked_ids": list(fusion.linked_ids),
        "reason": fusion.reason,
        "tacrep_type": fusion.tacrep_type,
        "sent": False,
    }


def build_snapshot(
    *,
    as_of: datetime,
    basemap: Mapping[str, object],
    routing: Mapping[str, object],
    sites: Sequence[Site],
    cells: Sequence[CellActivity],
    routes: Sequence[Msr],
    route_reports: Mapping[str, Sequence[Report]],
    suggested_fused: Sequence[Mapping[str, object]] = (),
    tracks: Sequence[GpsFix] = (),
    levels: LevelCatalog | None = None,
) -> dict[str, object]:
    evidence = _evidence_rows(sites, levels)
    return {
        "schema": SCHEMA_NAME,
        "schema_version": SCHEMA_VERSION,
        "type": "snapshot",
        "as_of": as_of.isoformat(),
        "basemap": dict(basemap),
        "routing": dict(routing),
        "sites": [_site_row(site, levels) for site in sites],
        "cells": [_cell_row(cell) for cell in cells],
        "routes": [
            _route_row(route, route_reports.get(route.name, ())) for route in routes
        ],
        "tracks": track_rows(tracks),
        "evidence": evidence,
        "suggested_fused": [dict(item) for item in suggested_fused],
    }


def diff_documents(
    previous: Mapping[str, object] | None,
    current: Mapping[str, object],
) -> dict[str, object] | None:
    """Return an update message, or None when nothing observable changed.

    The first document is a snapshot, so the caller sends ``current``
    itself when ``previous`` is None.
    """

    if previous is None:
        return None
    changes: list[dict[str, object]] = []
    changes.extend(_site_changes(previous.get("sites"), current.get("sites")))
    changes.extend(_cell_changes(previous.get("cells"), current.get("cells")))
    changes.extend(_route_changes(previous.get("routes"), current.get("routes")))
    changes.extend(_track_changes(previous.get("tracks"), current.get("tracks")))
    previous_fused = previous.get("suggested_fused")
    current_fused = current.get("suggested_fused")
    if not changes and previous_fused == current_fused:
        return None
    document = {
        "schema": SCHEMA_NAME,
        "schema_version": SCHEMA_VERSION,
        "type": "update",
        "as_of": current.get("as_of"),
        "basemap": current.get("basemap"),
        "routing": current.get("routing"),
        "changes": changes,
    }
    if previous_fused != current_fused:
        document["suggested_fused"] = current_fused
    return document


def landing_page(
    basemap: Mapping[str, object],
    routing: Mapping[str, object],
    public_base: str,
) -> dict[str, object]:
    root = public_base.rstrip("/")

    def href(path: str) -> str:
        return f"{root}{path}" if root else path

    links: list[dict[str, object]] = [
        {"rel": "self", "type": "application/json", "href": href("/ogc")},
        {
            "rel": "conformance",
            "type": "application/json",
            "href": href("/ogc/conformance"),
        },
        {"rel": "data", "type": "application/json", "href": href("/ogc/collections")},
    ]
    tile_url = basemap.get("tile_url")
    if isinstance(tile_url, str) and tile_url:
        links.append(
            {
                "rel": "tiles",
                "type": "image/png",
                "title": str(basemap.get("provider", "basemap")),
                "href": tile_url,
            }
        )
    return {
        "title": "NavTracker analytics",
        "description": (
            "Sites, hex activity, MSR corridors, GPS tracks, evidence, and travel estimates."
        ),
        "links": links,
        "basemap": dict(basemap),
        "routing": dict(routing),
    }


def items_document(
    name: str,
    sites: Sequence[Site],
    cells: Sequence[CellActivity],
    routes: Sequence[Msr],
    route_reports: Mapping[str, Sequence[Report]],
    bbox: tuple[float, float, float, float] | None,
    interval: tuple[datetime | None, datetime | None] | None = None,
    tracks: Sequence[GpsFix] = (),
    levels: LevelCatalog | None = None,
) -> dict[str, object]:
    """GeoJSON features for one OGC collection.

    ``bbox`` is west, south, east, north. ``interval`` is an inclusive
    start and end. A missing bound is open. Features with no timestamp
    are omitted when an interval is set.
    """

    features: list[dict[str, object]] = []
    if name == "sites":
        features.extend(
            _site_feature(site, levels)
            for site in sites
            if _point_inside(site.latitude, site.longitude, bbox)
            and _any_time((item.observed_at for item in site.evidence), interval)
        )
    elif name == "cells":
        features.extend(
            _cell_feature(cell)
            for cell in cells
            if _point_inside(cell.latitude, cell.longitude, bbox)
            and _any_time((report.observed_at for report in cell.reports), interval)
        )
    elif name == "routes":
        for route in routes:
            reports = route_reports.get(route.name, ())
            if route.points and _point_inside(route.points[0][0], route.points[0][1], bbox):
                if _any_time((report.observed_at for report in reports), interval):
                    features.append(_route_feature(route, reports))
    elif name == "evidence":
        features.extend(_evidence_features(sites, bbox, interval, levels))
    elif name == "tracks":
        features.extend(
            _track_feature(fix)
            for fix in dedupe_fixes(tracks)
            if _point_inside(fix.latitude, fix.longitude, bbox)
            and _matches_time(fix.timestamp, interval)
        )
    return {
        "type": "FeatureCollection",
        "numberReturned": len(features),
        "features": features,
    }


def parse_datetime_interval(text: str) -> tuple[datetime | None, datetime | None]:
    """Parse an OGC ``datetime`` instant or ``start/end`` interval."""

    raw = text.strip()
    if not raw:
        raise ValueError("datetime is empty")
    if "/" not in raw:
        instant = _parse_instant(raw)
        return instant, instant
    start_text, end_text = raw.split("/", 1)
    start = _open_bound(start_text)
    end = _open_bound(end_text)
    if start is not None and end is not None and start > end:
        raise ValueError("datetime interval is empty")
    return start, end


def parse_bbox(text: str) -> tuple[float, float, float, float]:
    parts = [part.strip() for part in text.split(",")]
    if len(parts) != 4:
        raise ValueError("bbox must be west,south,east,north")
    west, south, east, north = (float(part) for part in parts)
    if west > east or south > north:
        raise ValueError("bbox is empty")
    return west, south, east, north


def conformance() -> dict[str, object]:
    return {
        "conformsTo": [
            "http://www.opengis.net/spec/ogcapi-features-1/1.0/conf/core",
            "http://www.opengis.net/spec/ogcapi-features-1/1.0/conf/geojson",
        ]
    }


def _basemap(
    provider: str,
    tile_url: str,
    attribution: str,
    min_zoom: int,
    max_zoom: int,
    air_gapped: bool,
    view: tuple[float, float, int],
    mapbox_token: str,
) -> dict[str, object]:
    document: dict[str, object] = {
        "provider": provider,
        "tile_url": tile_url,
        "attribution": attribution,
        "min_zoom": min_zoom,
        "max_zoom": max_zoom,
        "air_gapped": air_gapped,
        "center_latitude": view[0],
        "center_longitude": view[1],
        "zoom": view[2],
    }
    if provider == "mapbox":
        document["style"] = MAPBOX_STYLE
        document["mapbox_token_configured"] = bool(mapbox_token)
    return document


def _mapbox_tiles(token: str) -> str:
    base = f"https://api.mapbox.com/styles/v1/{MAPBOX_STYLE}/tiles/{{z}}/{{x}}/{{y}}"
    if not token:
        return base
    return base + "?access_token=" + token


def _private_tiles(tile_url: str) -> bool:
    lowered = tile_url.lower()
    return (
        not lowered.startswith("http://")
        and not lowered.startswith("https://")
        or lowered.startswith("http://localhost")
        or lowered.startswith("http://127.0.0.1")
        or lowered.startswith("https://localhost")
    )


def site_fields(site: Site) -> dict[str, object]:
    """Site fields shared by HTTP, the WebSocket document, and OGC.

    ``status`` is operational activity. ``assessment`` is
    ``possible``, ``probable``, or ``confirmed``. ``verified`` on input
    is exported as ``confirmed``. ``confidence_band`` repeats that band
    and is deprecated.
    """

    band = normalize_level(site.status) or "possible"
    activity = site.activity_status
    if activity not in {"unknown", "active", "inactive"}:
        activity = "unknown"
    return {
        "id": site.name,
        "name": site.name,
        "latitude": site.latitude,
        "longitude": site.longitude,
        "status": activity,
        "assessment": band,
        "confidence": site.confidence,
        "confidence_band": band,
        "types": sorted(site.site_types),
        "bmoa": site.moa,
    }


def merge_level(
    document: dict[str, object],
    view: LevelView | None,
    *,
    assessment: bool = False,
) -> dict[str, object]:
    """Copy reported and assessed fields onto a site, correlation, or evidence row."""

    if view is None:
        return document
    document.update(level_document(view))
    if assessment and view.assessed_level:
        document["assessment"] = view.assessed_level
        document["confidence_band"] = view.assessed_level
    return document


def track_document(fix: GpsFix) -> dict[str, object]:
    """One GPS fix. ``speed`` is meters per second. ``lat`` and ``lon`` are degrees."""

    return {
        "vehicle_id": fix.vehicle_id,
        "lat": fix.latitude,
        "lon": fix.longitude,
        "timestamp": fix.timestamp.isoformat(),
        "speed": fix.speed_mps,
        "heading": fix.heading,
        "origin_id": fix.origin_id,
    }


def track_rows(fixes: Sequence[GpsFix]) -> list[dict[str, object]]:
    rows = [track_document(fix) for fix in dedupe_fixes(fixes)]
    rows.sort(key=lambda item: (str(item["vehicle_id"]), str(item["timestamp"])))
    return rows


def _site_row(site: Site, levels: LevelCatalog | None = None) -> dict[str, object]:
    document = site_fields(site)
    if levels is not None:
        merge_level(document, levels.for_site(site), assessment=True)
    document["origin"] = site.origin
    document["evidence"] = [_evidence_item(item, levels) for item in site.evidence]
    return document


def _evidence_item(item: Evidence, levels: LevelCatalog | None) -> dict[str, object]:
    row: dict[str, object] = {
        "kind": item.kind,
        "weight": item.weight,
        "detail": item.detail,
        "site_type": item.site_type,
        "source": item.source,
        "layer": item.layer,
        "message_id": item.message_id,
        "observed_at": None if item.observed_at is None else item.observed_at.isoformat(),
    }
    if levels is not None:
        merge_level(row, levels.for_evidence(item))
    return row


def _cell_row(cell: CellActivity) -> dict[str, object]:
    return {
        "cell_id": cell.cell_id,
        "score": cell.score,
        "hotspot": cell.hotspot,
        "on_msr": cell.on_msr,
        "latitude": cell.latitude,
        "longitude": cell.longitude,
        "boundary": [[point[0], point[1]] for point in cell.boundary],
        "reports": [report_document(report) for report in cell.reports],
    }


def _route_row(route: Msr, reports: Sequence[Report]) -> dict[str, object]:
    return {
        "name": route.name,
        "points": [[point[0], point[1]] for point in route.points],
        "reports": [report_document(report) for report in reports],
    }


def _track_feature(fix: GpsFix) -> dict[str, object]:
    return {
        "type": "Feature",
        "id": f"{fix.vehicle_id}|{fix.timestamp.isoformat()}",
        "geometry": {"type": "Point", "coordinates": [fix.longitude, fix.latitude]},
        "properties": track_document(fix),
    }


def _evidence_rows(
    sites: Sequence[Site],
    levels: LevelCatalog | None = None,
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for site in sites:
        for item in site.evidence:
            row: dict[str, object] = {
                "site": site.name,
                "kind": item.kind,
                "source": item.source,
                "layer": item.layer,
                "message_id": item.message_id,
                "detail": item.detail,
                "tacrep_type": item.tacrep_type,
                "block": item.block,
                "warning": item.warning,
                "linked_ids": list(item.linked_ids),
            }
            if levels is not None:
                merge_level(row, levels.for_evidence(item))
            rows.append(row)
    return rows


def _evidence_properties(
    site: Site,
    item: Evidence,
    levels: LevelCatalog | None,
) -> dict[str, object]:
    row: dict[str, object] = {
        "site": site.name,
        "kind": item.kind,
        "detail": item.detail,
        "source": item.source,
        "layer": item.layer,
        "message_id": item.message_id,
        "tacrep_type": item.tacrep_type,
        "block": item.block,
        "warning": item.warning,
        "linked_ids": list(item.linked_ids),
        "observed_at": None if item.observed_at is None else item.observed_at.isoformat(),
    }
    if levels is not None:
        merge_level(row, levels.for_evidence(item))
    return row


def _site_feature(site: Site, levels: LevelCatalog | None = None) -> dict[str, object]:
    geometry: dict[str, object]
    if site.latitude is None or site.longitude is None:
        geometry = {"type": "Point", "coordinates": []}
    else:
        geometry = {"type": "Point", "coordinates": [site.longitude, site.latitude]}
    return {
        "type": "Feature",
        "id": site.name,
        "geometry": geometry,
        "properties": _site_row(site, levels),
    }


def _cell_feature(cell: CellActivity) -> dict[str, object]:
    ring = [[point[1], point[0]] for point in cell.boundary]
    if ring and ring[0] != ring[-1]:
        ring.append(ring[0])
    return {
        "type": "Feature",
        "id": cell.cell_id,
        "geometry": {"type": "Polygon", "coordinates": [ring]},
        "properties": _cell_row(cell),
    }


def _route_feature(route: Msr, reports: Sequence[Report]) -> dict[str, object]:
    return {
        "type": "Feature",
        "id": route.name,
        "geometry": {
            "type": "LineString",
            "coordinates": [[point[1], point[0]] for point in route.points],
        },
        "properties": _route_row(route, reports),
    }


def _evidence_features(
    sites: Sequence[Site],
    bbox: tuple[float, float, float, float] | None,
    interval: tuple[datetime | None, datetime | None] | None,
    levels: LevelCatalog | None = None,
) -> list[dict[str, object]]:
    features: list[dict[str, object]] = []
    for site in sites:
        if site.latitude is None or site.longitude is None:
            continue
        if not _point_inside(site.latitude, site.longitude, bbox):
            continue
        for index, item in enumerate(site.evidence):
            if not _matches_time(item.observed_at, interval):
                continue
            features.append(
                {
                    "type": "Feature",
                    "id": f"{site.name}:{index}:{item.kind}",
                    "geometry": {
                        "type": "Point",
                        "coordinates": [site.longitude, site.latitude],
                    },
                    "properties": _evidence_properties(site, item, levels),
                }
            )
    return features


def _any_time(
    moments: Iterable[datetime | None],
    interval: tuple[datetime | None, datetime | None] | None,
) -> bool:
    if interval is None:
        return True
    return any(_matches_time(moment, interval) for moment in moments)


def _matches_time(
    when: datetime | None,
    interval: tuple[datetime | None, datetime | None] | None,
) -> bool:
    if interval is None:
        return True
    if when is None:
        return False
    moment = when if when.tzinfo is not None else when.replace(tzinfo=UTC)
    start, end = interval
    if start is not None and moment < start:
        return False
    if end is not None and moment > end:
        return False
    return True


def _open_bound(text: str) -> datetime | None:
    token = text.strip()
    if token in {"", ".."}:
        return None
    return _parse_instant(token)


def _parse_instant(text: str) -> datetime:
    token = text.strip()
    if token.endswith("Z"):
        token = token[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(token)
    except ValueError as exc:
        raise ValueError("datetime must be ISO-8601") from exc
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed


def _point_inside(
    latitude: float | None,
    longitude: float | None,
    bbox: tuple[float, float, float, float] | None,
) -> bool:
    if latitude is None or longitude is None:
        return bbox is None
    if bbox is None:
        return True
    west, south, east, north = bbox
    return west <= longitude <= east and south <= latitude <= north


def _site_changes(previous: object, current: object) -> list[dict[str, object]]:
    return _keyed_changes(
        previous,
        current,
        "name",
        "site",
        ("status", "assessment", "confidence", "types", "assessed_phrase", "disagreement"),
    )


def _cell_changes(previous: object, current: object) -> list[dict[str, object]]:
    return _keyed_changes(
        previous,
        current,
        "cell_id",
        "cell",
        ("score", "hotspot", "boundary"),
    )


def _route_changes(previous: object, current: object) -> list[dict[str, object]]:
    return _keyed_changes(previous, current, "name", "route", ("points", "reports"))


def _track_changes(previous: object, current: object) -> list[dict[str, object]]:
    before = _indexed(previous, _track_identity)
    after = _indexed(current, _track_identity)
    changes: list[dict[str, object]] = []
    for key, item in before.items():
        if key not in after:
            changes.append(
                {
                    "op": "remove",
                    "kind": "track",
                    "vehicle_id": item.get("vehicle_id"),
                    "timestamp": item.get("timestamp"),
                    "origin_id": item.get("origin_id", ""),
                }
            )
    for key, item in after.items():
        prior = before.get(key)
        if prior is None or prior != item:
            changes.append(
                {
                    "op": "upsert",
                    "kind": "track",
                    "vehicle_id": item.get("vehicle_id"),
                    "timestamp": item.get("timestamp"),
                    "item": item,
                }
            )
    return changes


def _track_identity(item: Mapping[str, object]) -> str | None:
    vehicle = item.get("vehicle_id")
    timestamp = item.get("timestamp")
    if not isinstance(vehicle, str) or not vehicle:
        return None
    if not isinstance(timestamp, str) or not timestamp:
        return None
    return f"{vehicle}|{timestamp}"


def _keyed_changes(
    previous: object,
    current: object,
    key_name: str,
    kind: str,
    fields: tuple[str, ...],
) -> list[dict[str, object]]:
    identity = _field_identity(key_name)
    before = _indexed(previous, identity)
    after = _indexed(current, identity)
    changes: list[dict[str, object]] = []
    for key in before:
        if key not in after:
            changes.append({"op": "remove", "kind": kind, key_name: key})
    for key, item in after.items():
        prior = before.get(key)
        if prior is None or any(prior.get(field) != item.get(field) for field in fields):
            changes.append({"op": "upsert", "kind": kind, key_name: key, "item": item})
    return changes


def _field_identity(key_name: str) -> Callable[[Mapping[str, object]], str | None]:
    def identity(item: Mapping[str, object]) -> str | None:
        value = item.get(key_name)
        if value is None or value == "":
            return None
        return str(value)

    return identity


def _indexed(
    rows: object,
    identity: Callable[[Mapping[str, object]], str | None],
) -> dict[str, dict[str, object]]:
    if not isinstance(rows, list):
        return {}
    found: dict[str, dict[str, object]] = {}
    for item in rows:
        if not isinstance(item, dict):
            continue
        key = identity(item)
        if key is None:
            continue
        found[key] = item
    return found
