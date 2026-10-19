"""Versioned output documents for WebSocket clients and the OGC landing page.

Basemap and routing metadata are advertised here. Provider ``none`` is
the air-gapped default: no tile host is implied. ``osm`` uses the public
OpenStreetMap tiles and needs no API key. ``xyz`` repeats the configured
URL unchanged, so a key belongs in the operator's environment, not in
this repository.

TransloadOracle's mapping and routing configuration was not readable
from this environment.
"""

from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime

from analytics.domain.grid import CellActivity
from analytics.domain.reference import Msr, Site
from analytics.domain.reports import Report, report_document
from analytics.domain.routing import VehicleProfile

SCHEMA_NAME = "navtracker.analytics"
SCHEMA_VERSION = 1
OSM_TILE_URL = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
OSM_ATTRIBUTION = "© OpenStreetMap contributors"


def basemap_document(
    provider: str,
    tile_url: str,
    attribution: str,
    min_zoom: int,
    max_zoom: int,
) -> dict[str, object]:
    if provider == "osm":
        return _basemap(
            "osm",
            OSM_TILE_URL,
            attribution or OSM_ATTRIBUTION,
            min_zoom,
            max_zoom,
            False,
        )
    if provider == "xyz":
        return _basemap(
            "xyz",
            tile_url,
            attribution,
            min_zoom,
            max_zoom,
            _private_tiles(tile_url),
        )
    return _basemap("none", "", attribution, min_zoom, max_zoom, True)


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


def build_snapshot(
    *,
    as_of: datetime,
    basemap: Mapping[str, object],
    routing: Mapping[str, object],
    sites: Sequence[Site],
    cells: Sequence[CellActivity],
    routes: Sequence[Msr],
    route_reports: Mapping[str, Sequence[Report]],
) -> dict[str, object]:
    evidence = _evidence_rows(sites)
    return {
        "schema": SCHEMA_NAME,
        "schema_version": SCHEMA_VERSION,
        "type": "snapshot",
        "as_of": as_of.isoformat(),
        "basemap": dict(basemap),
        "routing": dict(routing),
        "sites": [_site_row(site) for site in sites],
        "cells": [_cell_row(cell) for cell in cells],
        "routes": [
            _route_row(route, route_reports.get(route.name, ())) for route in routes
        ],
        "evidence": evidence,
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
    if not changes:
        return None
    return {
        "schema": SCHEMA_NAME,
        "schema_version": SCHEMA_VERSION,
        "type": "update",
        "as_of": current.get("as_of"),
        "basemap": current.get("basemap"),
        "routing": current.get("routing"),
        "changes": changes,
    }


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
        "description": "Sites, hex activity, MSR corridors, evidence, and travel estimates.",
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
) -> dict[str, object]:
    """GeoJSON features for one OGC collection.

    ``bbox`` is west, south, east, north. ``interval`` is an inclusive
    start and end. A missing bound is open. Features with no timestamp
    are omitted when an interval is set.
    """

    features: list[dict[str, object]] = []
    if name == "sites":
        features.extend(
            _site_feature(site)
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
        features.extend(_evidence_features(sites, bbox, interval))
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
) -> dict[str, object]:
    return {
        "provider": provider,
        "tile_url": tile_url,
        "attribution": attribution,
        "min_zoom": min_zoom,
        "max_zoom": max_zoom,
        "air_gapped": air_gapped,
    }


def _private_tiles(tile_url: str) -> bool:
    lowered = tile_url.lower()
    return (
        not lowered.startswith("http://")
        and not lowered.startswith("https://")
        or lowered.startswith("http://localhost")
        or lowered.startswith("http://127.0.0.1")
        or lowered.startswith("https://localhost")
    )


def _site_row(site: Site) -> dict[str, object]:
    return {
        "name": site.name,
        "types": sorted(site.site_types),
        "status": site.status,
        "confidence": site.confidence,
        "origin": site.origin,
        "latitude": site.latitude,
        "longitude": site.longitude,
        "evidence": [
            {
                "kind": item.kind,
                "weight": item.weight,
                "detail": item.detail,
                "site_type": item.site_type,
                "source": item.source,
                "layer": item.layer,
                "message_id": item.message_id,
                "observed_at": None
                if item.observed_at is None
                else item.observed_at.isoformat(),
            }
            for item in site.evidence
        ],
    }


def _cell_row(cell: CellActivity) -> dict[str, object]:
    return {
        "cell_id": cell.cell_id,
        "score": cell.score,
        "hotspot": cell.hotspot,
        "on_msr": cell.on_msr,
        "latitude": cell.latitude,
        "longitude": cell.longitude,
        "reports": [report_document(report) for report in cell.reports],
    }


def _route_row(route: Msr, reports: Sequence[Report]) -> dict[str, object]:
    return {
        "name": route.name,
        "reports": [report_document(report) for report in reports],
    }


def _evidence_rows(sites: Sequence[Site]) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for site in sites:
        for item in site.evidence:
            rows.append(
                {
                    "site": site.name,
                    "kind": item.kind,
                    "source": item.source,
                    "layer": item.layer,
                    "message_id": item.message_id,
                    "detail": item.detail,
                }
            )
    return rows


def _site_feature(site: Site) -> dict[str, object]:
    geometry: dict[str, object]
    if site.latitude is None or site.longitude is None:
        geometry = {"type": "Point", "coordinates": []}
    else:
        geometry = {"type": "Point", "coordinates": [site.longitude, site.latitude]}
    return {
        "type": "Feature",
        "id": site.name,
        "geometry": geometry,
        "properties": _site_row(site),
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
                    "properties": {
                        "site": site.name,
                        "kind": item.kind,
                        "detail": item.detail,
                        "source": item.source,
                        "layer": item.layer,
                        "message_id": item.message_id,
                        "observed_at": None
                        if item.observed_at is None
                        else item.observed_at.isoformat(),
                    },
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
    return _keyed_changes(previous, current, "name", "site", ("status", "confidence", "types"))


def _cell_changes(previous: object, current: object) -> list[dict[str, object]]:
    return _keyed_changes(previous, current, "cell_id", "cell", ("score", "hotspot"))


def _route_changes(previous: object, current: object) -> list[dict[str, object]]:
    return _keyed_changes(previous, current, "name", "route", ("reports",))


def _keyed_changes(
    previous: object,
    current: object,
    key_name: str,
    kind: str,
    fields: tuple[str, ...],
) -> list[dict[str, object]]:
    if not isinstance(previous, list) or not isinstance(current, list):
        return []
    before = {
        str(item.get(key_name)): item
        for item in previous
        if isinstance(item, dict) and item.get(key_name) is not None
    }
    changes: list[dict[str, object]] = []
    for item in current:
        if not isinstance(item, dict) or item.get(key_name) is None:
            continue
        key = str(item[key_name])
        prior = before.get(key)
        if prior is None or any(prior.get(field) != item.get(field) for field in fields):
            changes.append({"kind": kind, key_name: key, "item": item})
    return changes
