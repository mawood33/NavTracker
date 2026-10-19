"""Small JSON API. Construction does not bind a port."""

import json
from collections.abc import Mapping
from datetime import datetime
from typing import Protocol
from urllib.parse import parse_qs, unquote

from analytics.domain.aoi import AreaOfInterest
from analytics.domain.behavior import Mill, Slowdown, Stop
from analytics.domain.export import (
    geojson_document,
    heatmap_document,
    kml_document,
    kml_network_link,
)
from analytics.domain.grid import CellActivity
from analytics.domain.outputs import (
    conformance,
    items_document,
    landing_page,
    parse_bbox,
    parse_datetime_interval,
)
from analytics.domain.reference import Evidence, Msr, Site
from analytics.domain.reports import Report
from analytics.domain.routing import RoutingError


class AnalyticsSnapshot(Protocol):
    def health(self) -> Mapping[str, object]:
        """Liveness payload."""

    def events(self) -> list[object]:
        """Detected behavior events."""

    def areas(self) -> list[AreaOfInterest]:
        """Classified areas of interest."""

    def sites(self) -> list[Site]:
        """Loaded known sites."""

    def routes(self) -> list[Msr]:
        """Loaded MSRs."""

    def cells(self) -> list[CellActivity]:
        """Hex cells with activity scores."""

    def msr_reports(self) -> Mapping[str, tuple[Report, ...]]:
        """Reports correlated to each MSR name."""

    def corridor_meters(self) -> float:
        """MSR corridor buffer in meters."""

    def basemap(self) -> Mapping[str, object]:
        """Configured tile provider. No API key is included."""

    def routing_info(self) -> Mapping[str, object]:
        """Configured routing engine and vehicle profiles."""

    def route_between(
        self,
        origin_name: str,
        destination_name: str,
        profile_name: str,
    ) -> Mapping[str, object]:
        """Route and ETA between two loaded sites."""

    def api_sites(self) -> list[dict[str, object]]:
        """Sites in the TransloadOracle web shape."""

    def api_route(
        self,
        origin_id: str,
        destination_id: str,
        vehicle_type: str,
        max_speed_mph: float | None,
    ) -> Mapping[str, object]:
        """One provider response for POST /api/routes."""

    def ingest_event(
        self,
        payload: Mapping[str, object],
        source_id: str = "event",
    ) -> Mapping[str, object]:
        """Accept one JSON event."""

    def ingest_tacrep_body(self, body: str, source_id: str) -> Mapping[str, object]:
        """Accept one TACREP line."""

    def ingest_batch(self, payload: Mapping[str, object]) -> Mapping[str, object]:
        """Accept a batch of events."""

    def correlations_document(self) -> Mapping[str, object]:
        """Correlation groups."""

    def activities_document(self) -> Mapping[str, object]:
        """Current activities."""

    def advance_activity(self, activity_id: str) -> Mapping[str, object]:
        """Move one activity forward one state."""

    def verify_site(self, name: str) -> Mapping[str, object]:
        """Mark a site confirmed from manual or truth data."""

    def public_base(self) -> str:
        """Absolute origin for OGC links, or empty for relative links."""

    def reload_reference(self) -> Mapping[str, int]:
        """Reread the reference directory."""

    def save_upload(self, filename: str, body: bytes) -> Mapping[str, int]:
        """Store one reference file and reload."""


def route_request(
    method: str,
    path: str,
    body: bytes,
    headers: Mapping[str, str],
    snapshot: AnalyticsSnapshot,
) -> tuple[int, dict[str, object] | str, str]:
    """Return status, a JSON object or text, and a content type.

    No socket is opened.
    """

    path_only, _, query = path.partition("?")
    target = path_only.rstrip("/") or "/"
    params = {key: values[-1] for key, values in parse_qs(query).items()}
    if method == "GET" and target == "/health":
        return _json(200, dict(snapshot.health()))
    if method == "GET" and target == "/events":
        return _json(200, {"events": snapshot.events()})
    if method == "GET" and target == "/aois":
        return _json(200, {"areas": [_area(area) for area in snapshot.areas()]})
    if method == "GET" and target == "/sites":
        return _json(200, {"sites": [_site(site) for site in snapshot.sites()]})
    if method == "GET" and target == "/msrs":
        return _json(200, {"msrs": [_msr(route) for route in snapshot.routes()]})
    if method == "GET" and target == "/cells":
        return _json(200, {"cells": [_cell(cell) for cell in snapshot.cells()]})
    if method == "GET" and target == "/geojson":
        return (
            200,
            geojson_document(
                snapshot.cells(),
                snapshot.sites(),
                snapshot.routes(),
                snapshot.corridor_meters(),
            ),
            "application/geo+json",
        )
    if method == "GET" and target == "/heatmap":
        return _json(200, heatmap_document(snapshot.cells()))
    if method == "GET" and target == "/routing":
        return _route_response(params, snapshot)
    if method == "GET" and target == "/api/sites":
        return _json(200, {"sites": snapshot.api_sites()})
    if method == "POST" and target == "/api/routes":
        return _api_routes(body, snapshot)
    if method == "POST" and target.startswith("/api/sites/") and target.endswith("/verify"):
        return _verify_site(target, snapshot)
    if method == "POST" and target == "/v1/events":
        return _ingest_events(body, snapshot, batch=False, tacrep=False)
    if method == "POST" and target == "/v1/events/tacrep":
        return _ingest_events(body, snapshot, batch=False, tacrep=True)
    if method == "POST" and target == "/v1/events/batch":
        return _ingest_events(body, snapshot, batch=True, tacrep=False)
    if method == "GET" and target == "/v1/events/correlations":
        return _json(200, dict(snapshot.correlations_document()))
    if method == "GET" and target == "/v1/activities":
        return _json(200, dict(snapshot.activities_document()))
    if method == "GET" and target == "/v1/activities/stream":
        payload = snapshot.activities_document()
        data = json.dumps(payload, separators=(",", ":"))
        return 200, f"data: {data}\n\n", "text/event-stream"
    if method == "POST" and target.startswith("/v1/activities/") and target.endswith("/advance"):
        return _advance_activity(target, snapshot)
    if method == "GET" and target == "/ogc":
        return _json(
            200,
            landing_page(snapshot.basemap(), snapshot.routing_info(), snapshot.public_base()),
        )
    if method == "GET" and target == "/ogc/conformance":
        return _json(200, conformance())
    if method == "GET" and target == "/ogc/collections":
        return _json(200, {"collections": [_collection(name) for name in _COLLECTIONS]})
    if method == "GET" and target.startswith("/ogc/collections/"):
        return _ogc_collection(target, params, snapshot)
    if method == "GET" and target == "/kml/networklink":
        href = snapshot.public_base().rstrip("/") + "/kml"
        return 200, kml_network_link(href), "application/vnd.google-earth.kml+xml"
    if method == "GET" and target == "/kml":
        document = kml_document(
            snapshot.cells(),
            snapshot.sites(),
            snapshot.routes(),
            snapshot.corridor_meters(),
        )
        return 200, document, "application/vnd.google-earth.kml+xml"
    if method == "POST" and target == "/reference/reload":
        return _json(200, dict(snapshot.reload_reference()))
    if method == "POST" and target == "/reference/upload":
        filename = headers.get("x-filename", "").strip()
        if not filename or "/" in filename or "\\" in filename or filename.startswith("."):
            return _json(400, {"error": "X-Filename must be a single file name"})
        return _json(200, dict(snapshot.save_upload(filename, body)))
    return _json(404, {"error": "not found"})


_COLLECTIONS = ("sites", "cells", "routes", "evidence", "itineraries")


def _api_routes(
    body: bytes,
    snapshot: AnalyticsSnapshot,
) -> tuple[int, dict[str, object] | str, str]:
    payload = _object(body)
    if payload is None:
        return _error(400, "Expected a JSON object.", "invalid_json")
    origin = str(payload.get("originSiteId", "") or "").strip()
    destination = str(payload.get("destinationSiteId", "") or "").strip()
    vehicle = str(payload.get("vehicleType", "truck") or "truck").strip() or "truck"
    speed = payload.get("maxSpeedMph")
    mph: float | None
    if speed is None or speed == "":
        mph = None
    elif isinstance(speed, bool) or not isinstance(speed, (int, float)):
        return _error(400, "maxSpeedMph must be a number.", "invalid_speed")
    else:
        mph = float(speed)
    if not origin or not destination:
        return _error(400, "originSiteId and destinationSiteId are required.", "missing_site")
    try:
        return _json(200, dict(snapshot.api_route(origin, destination, vehicle, mph)))
    except RoutingError as exc:
        status = 400 if exc.code in {"rail_unsupported", "invalid_speed", "missing_site"} else 404
        return _error(status, str(exc), exc.code)


def _verify_site(
    target: str,
    snapshot: AnalyticsSnapshot,
) -> tuple[int, dict[str, object] | str, str]:
    name = unquote(target[len("/api/sites/") : -len("/verify")]).strip("/")
    if not name:
        return _error(404, "Unknown site.", "not_found")
    try:
        return _json(200, dict(snapshot.verify_site(name)))
    except KeyError:
        return _error(404, "Unknown site.", "not_found")


def _advance_activity(
    target: str,
    snapshot: AnalyticsSnapshot,
) -> tuple[int, dict[str, object] | str, str]:
    activity_id = unquote(target[len("/v1/activities/") : -len("/advance")]).strip("/")
    try:
        return _json(200, dict(snapshot.advance_activity(activity_id)))
    except KeyError:
        return _error(404, "Unknown activity.", "not_found")


def _ingest_events(
    body: bytes,
    snapshot: AnalyticsSnapshot,
    *,
    batch: bool,
    tacrep: bool,
) -> tuple[int, dict[str, object] | str, str]:
    try:
        if tacrep:
            text = body.decode("utf-8")
            payload = _object(body)
            if payload is not None and isinstance(payload.get("tacrep"), str):
                text = str(payload["tacrep"])
                source = str(payload.get("source_id", "tacrep") or "tacrep")
            else:
                source = "tacrep"
            return _json(200, dict(snapshot.ingest_tacrep_body(text, source)))
        payload = _object(body)
        if payload is None:
            return _error(400, "Expected a JSON object.", "invalid_json")
        if batch:
            return _json(200, dict(snapshot.ingest_batch(payload)))
        return _json(200, dict(snapshot.ingest_event(payload)))
    except ValueError as exc:
        return _error(400, str(exc), "invalid_event")


def _object(body: bytes) -> dict[str, object] | None:
    if not body:
        return None
    try:
        payload = json.loads(body.decode("utf-8"))
    except ValueError:
        return None
    if isinstance(payload, dict):
        return payload
    return None


def _error(
    status: int,
    message: str,
    code: str,
) -> tuple[int, dict[str, object] | str, str]:
    return status, {"error": message, "code": code}, "application/json"


def _route_response(
    params: Mapping[str, str],
    snapshot: AnalyticsSnapshot,
) -> tuple[int, dict[str, object] | str, str]:
    origin = params.get("from_site", "").strip()
    destination = params.get("to_site", "").strip()
    profile = params.get("profile", "truck").strip() or "truck"
    if not origin or not destination:
        return _json(400, {"error": "from_site and to_site are required"})
    try:
        return _json(200, dict(snapshot.route_between(origin, destination, profile)))
    except RoutingError as exc:
        return _json(404, {"error": str(exc)})


def _ogc_collection(
    target: str,
    params: Mapping[str, str],
    snapshot: AnalyticsSnapshot,
) -> tuple[int, dict[str, object] | str, str]:
    parts = [part for part in target.split("/") if part]
    # ogc collections {name} [items] [id]
    if len(parts) < 3 or parts[2] not in _COLLECTIONS:
        return _json(404, {"error": "unknown collection"})
    name = parts[2]
    if len(parts) == 3:
        return _json(200, _collection(name))
    if len(parts) >= 4 and parts[3] == "items":
        if name == "itineraries":
            if not params.get("from_site") and not params.get("to_site"):
                return _json(200, {"type": "FeatureCollection", "features": []})
            status, payload, content_type = _route_response(params, snapshot)
            if status != 200 or not isinstance(payload, dict):
                return status, payload, content_type
            geometry = payload.get("geometry")
            properties = {
                key: value for key, value in payload.items() if key != "geometry"
            }
            return _json(
                200,
                {
                    "type": "FeatureCollection",
                    "features": [
                        {
                            "type": "Feature",
                            "geometry": geometry,
                            "properties": properties,
                        }
                    ],
                },
            )
        bbox = None
        if "bbox" in params:
            try:
                bbox = parse_bbox(params["bbox"])
            except ValueError as exc:
                return _json(400, {"error": str(exc)})
        interval = None
        if "datetime" in params:
            try:
                interval = parse_datetime_interval(params["datetime"])
            except ValueError as exc:
                return _json(400, {"error": str(exc)})
        return _json(
            200,
            items_document(
                name,
                snapshot.sites(),
                snapshot.cells(),
                snapshot.routes(),
                snapshot.msr_reports(),
                bbox,
                interval,
            ),
        )
    return _json(404, {"error": "not found"})


def _collection(name: str) -> dict[str, object]:
    return {
        "id": name,
        "title": name,
        "itemType": "feature",
        "links": [
            {
                "rel": "items",
                "type": "application/geo+json",
                "href": f"/ogc/collections/{name}/items",
            }
        ],
    }


def _json(status: int, payload: dict[str, object]) -> tuple[int, dict[str, object] | str, str]:
    return status, payload, "application/json"


def encode_response(
    status: int,
    payload: dict[str, object] | str,
    content_type: str = "application/json",
) -> bytes:
    if isinstance(payload, str):
        body = payload.encode("utf-8")
    else:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    reason = "OK" if status == 200 else "ERROR"
    header = (
        f"HTTP/1.1 {status} {reason}\r\n"
        f"Content-Type: {content_type}\r\n"
        f"Content-Length: {len(body)}\r\n"
        "Connection: close\r\n\r\n"
    )
    return header.encode("ascii") + body


def event_document(
    slowdowns: list[Slowdown],
    stops: list[Stop],
    mills: list[Mill],
    areas: list[AreaOfInterest],
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "events": (
            [_slowdown(item) for item in slowdowns]
            + [_stop(item) for item in stops]
            + [_mill(item) for item in mills]
        ),
        "areas": [_area(area) for area in areas],
    }


def _slowdown(item: Slowdown) -> dict[str, object]:
    return {
        "kind": "slowdown",
        "vehicle_id": item.vehicle_id,
        "timestamp": _iso(item.timestamp),
        "latitude": item.latitude,
        "longitude": item.longitude,
        "previous_speed_mps": item.previous_speed_mps,
        "speed_mps": item.speed_mps,
    }


def _stop(item: Stop) -> dict[str, object]:
    return {
        "kind": "stop",
        "vehicle_id": item.vehicle_id,
        "start": _iso(item.start),
        "end": _iso(item.end),
        "dwell_seconds": item.dwell_seconds,
        "latitude": item.latitude,
        "longitude": item.longitude,
    }


def _mill(item: Mill) -> dict[str, object]:
    return {
        "kind": "mill",
        "vehicle_ids": list(item.vehicle_ids),
        "start": _iso(item.start),
        "end": _iso(item.end),
        "dwell_seconds": item.dwell_seconds,
        "latitude": item.latitude,
        "longitude": item.longitude,
    }


def _area(area: AreaOfInterest) -> dict[str, object]:
    return {
        "classification": area.classification,
        "latitude": area.latitude,
        "longitude": area.longitude,
        "visit_count": area.visit_count,
        "vehicle_ids": list(area.vehicle_ids),
        "total_dwell_seconds": area.total_dwell_seconds,
        "median_dwell_seconds": area.median_dwell_seconds,
        "matched_site": area.matched_site,
        "matched_site_types": sorted(area.matched_site_types),
        "supported_types": sorted(area.supported_types),
        "matched_msr": area.matched_msr,
        "reasons": list(area.reasons),
    }


def _site(site: Site) -> dict[str, object]:
    return {
        "name": site.name,
        "types": sorted(site.site_types),
        "status": site.status,
        "confidence": site.confidence,
        "origin": site.origin,
        "latitude": site.latitude,
        "longitude": site.longitude,
        "moa": site.moa,
        "complex": site.complex_name,
        "mgrs": site.mgrs,
        "polygon": [list(point) for point in site.polygon],
        "evidence": [_evidence(item) for item in site.evidence],
    }


def _evidence(item: Evidence) -> dict[str, object]:
    return {
        "kind": item.kind,
        "weight": item.weight,
        "detail": item.detail,
        "site_type": item.site_type,
        "observed_at": None if item.observed_at is None else _iso(item.observed_at),
        "source": item.source,
        "layer": item.layer,
        "message_id": item.message_id,
    }


def _cell(cell: CellActivity) -> dict[str, object]:
    return {
        "cell_id": cell.cell_id,
        "latitude": cell.latitude,
        "longitude": cell.longitude,
        "score": cell.score,
        "hotspot": cell.hotspot,
        "stops": cell.stops,
        "dwell_seconds": cell.dwell_seconds,
        "vehicles": list(cell.vehicle_ids),
        "revisits": cell.revisits,
        "milling": cell.milling,
        "on_msr": cell.on_msr,
        "boundary": [list(point) for point in cell.boundary],
    }


def _msr(route: Msr) -> dict[str, object]:
    return {
        "name": route.name,
        "length": route.length_text,
        "points": [list(point) for point in route.points],
    }


def _iso(value: datetime) -> str:
    return value.isoformat()
