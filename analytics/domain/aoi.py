from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from analytics.domain.behavior import Stop
from analytics.domain.geo import haversine_meters
from analytics.domain.reference import Msr, Site, match_msr, match_site
from analytics.domain.thresholds import AnalysisThresholds
from analytics.domain.tracks import seconds_between


@dataclass(frozen=True)
class AreaOfInterest:
    """A cluster of stops and the site types the heuristics support.

    ``classification`` is the sorted types joined with commas, or
    ``unknown`` when no type matched. ``supported_types`` is that same
    set. ``matched_site_types`` is the set already recorded on a known
    site, which may be empty.
    """

    classification: str
    supported_types: frozenset[str]
    latitude: float
    longitude: float
    visit_count: int
    vehicle_ids: tuple[str, ...]
    total_dwell_seconds: float
    median_dwell_seconds: float
    matched_site: str | None
    matched_site_types: frozenset[str]
    matched_msr: str | None
    reasons: tuple[str, ...]


def derive_areas(
    stops: Sequence[Stop],
    sites: Sequence[Site],
    msrs: Sequence[Msr],
    thresholds: AnalysisThresholds,
) -> list[AreaOfInterest]:
    """Cluster stops and classify each cluster."""

    areas: list[AreaOfInterest] = []
    for cluster in _cluster(stops, thresholds.aoi_radius_meters):
        areas.append(_classify(cluster, sites, msrs, thresholds))
    return areas


def _cluster(stops: Sequence[Stop], radius_meters: float) -> list[list[Stop]]:
    clusters: list[list[Stop]] = []
    for stop in stops:
        placed = False
        for cluster in clusters:
            centroid_lat = sum(item.latitude for item in cluster) / len(cluster)
            centroid_lon = sum(item.longitude for item in cluster) / len(cluster)
            distance = haversine_meters(
                stop.latitude,
                stop.longitude,
                centroid_lat,
                centroid_lon,
            )
            if distance <= radius_meters:
                cluster.append(stop)
                placed = True
                break
        if not placed:
            clusters.append([stop])
    return clusters


def _classify(
    cluster: list[Stop],
    sites: Sequence[Site],
    msrs: Sequence[Msr],
    thresholds: AnalysisThresholds,
) -> AreaOfInterest:
    latitude = sum(stop.latitude for stop in cluster) / len(cluster)
    longitude = sum(stop.longitude for stop in cluster) / len(cluster)
    vehicles = tuple(dict.fromkeys(stop.vehicle_id for stop in cluster))
    dwells = sorted(stop.dwell_seconds for stop in cluster)
    median = dwells[len(dwells) // 2]
    total = sum(dwells)
    visits = _visit_count(cluster, thresholds.revisit_gap_seconds)
    site = match_site(
        latitude, longitude, sites, thresholds.site_match_meters
    )
    route = match_msr(
        latitude, longitude, msrs, thresholds.msr_corridor_meters
    )
    known = site.site_types if site is not None else frozenset()
    supported: set[str] = set(known)
    reasons: list[str] = []
    if known:
        reasons.append("known site types " + ", ".join(sorted(known)))
    if (
        len(vehicles) >= thresholds.transload_min_vehicles
        and total >= thresholds.transload_min_dwell_seconds
        and route is not None
    ):
        supported.add("transload")
        reasons.append("several vehicles dwelling on an MSR")
    if (
        len(vehicles) <= thresholds.hide_max_vehicles
        and max(dwells) >= thresholds.hide_min_dwell_seconds
        and route is None
    ):
        supported.add("hide")
        reasons.append("long dwell off the MSR network")
    if (
        visits >= thresholds.launch_min_visits
        and thresholds.launch_min_dwell_seconds
        <= median
        <= thresholds.launch_max_dwell_seconds
    ):
        supported.add("launch")
        reasons.append("repeated short dwells")
    if not supported:
        reasons.append("no hide, launch, or transload rule matched")
    if site is not None:
        reasons.append(f"matched site {site.name}")
    if route is not None:
        reasons.append(f"on {route.name}")
    else:
        reasons.append("off MSR")
    ordered = tuple(sorted(supported))
    return AreaOfInterest(
        classification=",".join(ordered) if ordered else "unknown",
        supported_types=frozenset(supported),
        latitude=latitude,
        longitude=longitude,
        visit_count=visits,
        vehicle_ids=vehicles,
        total_dwell_seconds=total,
        median_dwell_seconds=median,
        matched_site=None if site is None else site.name,
        matched_site_types=known,
        matched_msr=None if route is None else route.name,
        reasons=tuple(reasons),
    )


def _visit_count(cluster: Sequence[Stop], revisit_gap_seconds: float) -> int:
    """Count stops, collapsing same-vehicle stops that are not separated."""

    per_vehicle: dict[str, list[Stop]] = {}
    for stop in sorted(cluster, key=lambda item: (item.vehicle_id, item.start)):
        per_vehicle.setdefault(stop.vehicle_id, []).append(stop)
    visits = 0
    for stops in per_vehicle.values():
        visits += 1
        previous: datetime = stops[0].end
        for stop in stops[1:]:
            if seconds_between(previous, stop.start) >= revisit_gap_seconds:
                visits += 1
            if stop.end > previous:
                previous = stop.end
    return visits
