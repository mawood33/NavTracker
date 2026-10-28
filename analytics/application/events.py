"""Behavior event documents shared by the service and the HTTP adapter."""

from datetime import datetime

from analytics.domain.aoi import AreaOfInterest
from analytics.domain.behavior import Mill, Slowdown, Stop
from navtracker_common.timeutil import format_zulu


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
        "areas": [area_document(area) for area in areas],
    }


def area_document(area: AreaOfInterest) -> dict[str, object]:
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


def _iso(value: datetime) -> str:
    return format_zulu(value)
