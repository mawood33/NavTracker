"""Map documents for a frontend. Geometries use GeoJSON lon/lat order."""

from collections.abc import Sequence
from xml.sax.saxutils import escape

from analytics.domain.geo import corridor_polygon
from analytics.domain.grid import CellActivity
from analytics.domain.reference import Msr, Site


def geojson_document(
    cells: Sequence[CellActivity],
    sites: Sequence[Site],
    msrs: Sequence[Msr],
    corridor_meters: float,
) -> dict[str, object]:
    """FeatureCollection of hex cells, sites, and MSR corridor polygons."""

    features: list[dict[str, object]] = []
    for cell in cells:
        features.append(_cell_feature(cell))
    for site in sites:
        features.append(_site_feature(site))
    for route in msrs:
        feature = _msr_feature(route, corridor_meters)
        if feature is not None:
            features.append(feature)
    return {"type": "FeatureCollection", "features": features}


def kml_network_link(href: str) -> str:
    """KML NetworkLink that refreshes the analytics ``/kml`` document."""

    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<kml xmlns="http://www.opengis.net/kml/2.2"><Document>'
        "<NetworkLink><name>NavTracker analytics</name><Link><href>"
        + escape(href)
        + "</href><refreshMode>onInterval</refreshMode>"
        "<refreshInterval>30</refreshInterval></Link></NetworkLink>"
        "</Document></kml>"
    )


def heatmap_document(cells: Sequence[CellActivity]) -> dict[str, object]:
    """Cell centers and a 0..1 weight for a heatmap layer."""

    peak = max((cell.score for cell in cells), default=0.0)
    rows: list[dict[str, object]] = []
    for cell in cells:
        weight = 0.0 if peak <= 0 else cell.score / peak
        rows.append(
            {
                "cell_id": cell.cell_id,
                "latitude": cell.latitude,
                "longitude": cell.longitude,
                "score": cell.score,
                "weight": weight,
                "hotspot": cell.hotspot,
                "on_msr": cell.on_msr,
            }
        )
    return {"max_score": peak, "cells": rows}


def kml_document(
    cells: Sequence[CellActivity],
    sites: Sequence[Site],
    msrs: Sequence[Msr],
    corridor_meters: float,
) -> str:
    """KML folder of the same three layers."""

    parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<kml xmlns="http://www.opengis.net/kml/2.2"><Document>',
        "<name>NavTracker activity</name>",
        "<Folder><name>Cells</name>",
    ]
    for cell in cells:
        parts.append(_kml_polygon(cell.cell_id, _cell_description(cell), cell.boundary))
    parts.append("</Folder><Folder><name>Sites</name>")
    for site in sites:
        if site.latitude is None or site.longitude is None:
            continue
        parts.append(
            _kml_point(
                site.name,
                _site_description(site),
                site.latitude,
                site.longitude,
            )
        )
    parts.append("</Folder><Folder><name>MSRs</name>")
    for route in msrs:
        ring = corridor_polygon(route.points, corridor_meters)
        if ring:
            parts.append(_kml_polygon(route.name, f"corridor {corridor_meters:.0f} m", ring))
    parts.append("</Folder></Document></kml>")
    return "".join(parts)


def _cell_feature(cell: CellActivity) -> dict[str, object]:
    return {
        "type": "Feature",
        "geometry": {"type": "Polygon", "coordinates": [_ring(cell.boundary)]},
        "properties": {
            "layer": "cell",
            "cell_id": cell.cell_id,
            "score": cell.score,
            "hotspot": cell.hotspot,
            "stops": cell.stops,
            "dwell_seconds": cell.dwell_seconds,
            "vehicles": list(cell.vehicle_ids),
            "revisits": cell.revisits,
            "milling": cell.milling,
            "on_msr": cell.on_msr,
        },
    }


def _site_feature(site: Site) -> dict[str, object]:
    if site.polygon:
        geometry: dict[str, object] = {
            "type": "Polygon",
            "coordinates": [_ring(site.polygon)],
        }
    elif site.latitude is not None and site.longitude is not None:
        geometry = {
            "type": "Point",
            "coordinates": [site.longitude, site.latitude],
        }
    else:
        geometry = {"type": "Point", "coordinates": []}
    return {
        "type": "Feature",
        "geometry": geometry,
        "properties": {
            "layer": "site",
            "name": site.name,
            "types": sorted(site.site_types),
            "status": site.status,
            "confidence": site.confidence,
            "origin": site.origin,
        },
    }


def _msr_feature(route: Msr, corridor_meters: float) -> dict[str, object] | None:
    ring = corridor_polygon(route.points, corridor_meters)
    if not ring:
        return None
    return {
        "type": "Feature",
        "geometry": {"type": "Polygon", "coordinates": [_ring(ring)]},
        "properties": {
            "layer": "msr",
            "name": route.name,
            "corridor_meters": corridor_meters,
            "length": route.length_text,
        },
    }


def _ring(points: Sequence[tuple[float, float]]) -> list[list[float]]:
    coordinates = [[point[1], point[0]] for point in points]
    if coordinates and coordinates[0] != coordinates[-1]:
        coordinates.append(coordinates[0])
    return coordinates


def _cell_description(cell: CellActivity) -> str:
    return (
        f"score {cell.score:.2f}; stops {cell.stops}; "
        f"hotspot {cell.hotspot}; on_msr {cell.on_msr}"
    )


def _site_description(site: Site) -> str:
    types = ",".join(sorted(site.site_types)) or "untyped"
    return f"{site.status} {site.confidence:.2f} {types} {site.origin}"


def _kml_point(name: str, description: str, latitude: float, longitude: float) -> str:
    return (
        "<Placemark><name>"
        + escape(name)
        + "</name><description>"
        + escape(description)
        + "</description><Point><coordinates>"
        + f"{longitude},{latitude},0"
        + "</coordinates></Point></Placemark>"
    )


def _kml_polygon(
    name: str,
    description: str,
    ring: Sequence[tuple[float, float]],
) -> str:
    coordinates = " ".join(f"{point[1]},{point[0]},0" for point in ring)
    if ring:
        coordinates += f" {ring[0][1]},{ring[0][0]},0"
    return (
        "<Placemark><name>"
        + escape(name)
        + "</name><description>"
        + escape(description)
        + "</description><Polygon><outerBoundaryIs><LinearRing><coordinates>"
        + coordinates
        + "</coordinates></LinearRing></outerBoundaryIs></Polygon></Placemark>"
    )
