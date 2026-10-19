"""Route between two sites and estimate travel time.

TransloadOracle's extracted routing contract is the default: provider
``osrm`` or ``mapbox``, public OSRM at ``https://router.project-osrm.org``,
and ETA minutes ``max(engine_minutes, miles / maxSpeedMph * 60)`` with
``maxSpeedMph`` capped at 120. Vehicle types are ``truck``,
``intermodal``, and ``rail`` (rail is rejected). Valhalla, GraphHopper,
the MSR corridor fallback, and the light/heavy size profiles are
NavTracker extras. ``eta_factor`` multiplies that ETA for the size
profiles and is 1.0 for truck and intermodal.

OSRM and Mapbox receive a driving profile, not height, weight, or
hazmat. Valhalla receives costing and top speed only.
"""

import heapq
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace

from analytics.domain.geo import closest_point_on_polyline, haversine_meters
from analytics.domain.reference import Msr


class RoutingError(Exception):
    """No route could be produced for these sites."""

    def __init__(self, message: str, code: str = "routing_error"):
        super().__init__(message)
        self.code = code


METERS_PER_MILE = 1609.344
MAX_SPEED_MPH = 120.0
OSRM_PUBLIC_BASE_URL = "https://router.project-osrm.org"
MAPBOX_DIRECTIONS_BASE = "https://api.mapbox.com"
ROUTING_TIMEOUT_SECONDS = 15.0


@dataclass(frozen=True)
class VehicleProfile:
    """Local ETA inputs. Engines do not receive the weight class."""

    name: str
    max_speed_mps: float = 18.0
    weight_class: str = "heavy"
    vehicle_type: str = "truck"
    eta_factor: float = 1.0
    osrm_profile: str = "driving"
    valhalla_costing: str = "truck"
    graphhopper_profile: str = "truck"
    mapbox_profile: str = "driving"
    max_speed_mph: float = 0.0


@dataclass(frozen=True)
class RouteLeg:
    """One path. Geometry points are (latitude, longitude)."""

    engine: str
    distance_meters: float
    duration_seconds: float
    geometry: tuple[tuple[float, float], ...]
    profile_name: str
    constrained_to_msr: bool
    engine_duration_seconds: float | None = None
    cached: bool = False
    note: str = ""


def default_profiles() -> tuple[VehicleProfile, ...]:
    """Truck and intermodal match the extracted vehicle types.

    ``light`` and ``heavy`` are extra size profiles. ``heavy`` keeps an
    ETA factor above 1. Rail is not a profile; requesting it raises.
    """

    return (
        VehicleProfile(name="truck", vehicle_type="truck"),
        VehicleProfile(name="intermodal", vehicle_type="intermodal"),
        VehicleProfile(
            name="light",
            max_speed_mps=25.0,
            weight_class="light",
            vehicle_type="car",
            eta_factor=1.0,
            osrm_profile="driving",
            valhalla_costing="auto",
            graphhopper_profile="car",
            mapbox_profile="driving",
        ),
        VehicleProfile(
            name="heavy",
            weight_class="heavy",
            vehicle_type="truck",
            eta_factor=1.4,
        ),
    )


def reject_unsupported_vehicle(vehicle_type: str) -> None:
    """Rail is named by TransloadOracle and is not routed."""

    if vehicle_type.strip().lower() == "rail":
        raise RoutingError("Rail routing is not supported.", "rail_unsupported")


def limit_speed(profile: VehicleProfile, max_speed_mph: float | None) -> VehicleProfile:
    """Cap a requested speed at 120 mph and store both units."""

    if max_speed_mph is None:
        return profile
    if max_speed_mph <= 0:
        raise RoutingError("maxSpeedMph must be positive.", "invalid_speed")
    capped = min(max_speed_mph, MAX_SPEED_MPH)
    return replace(
        profile,
        max_speed_mph=capped,
        max_speed_mps=capped * METERS_PER_MILE / 3600,
    )


def apply_profile_eta(leg: RouteLeg, profile: VehicleProfile) -> RouteLeg:
    """ETA is the slower of the engine time and the capped speed time."""

    engine_seconds = (
        leg.engine_duration_seconds
        if leg.engine_duration_seconds is not None
        else leg.duration_seconds
    )
    return replace(
        leg,
        duration_seconds=eta_seconds(leg.distance_meters, profile, engine_seconds),
        profile_name=profile.name,
        engine_duration_seconds=engine_seconds,
    )


def eta_seconds(
    distance_meters: float,
    profile: VehicleProfile,
    engine_seconds: float | None = None,
) -> float:
    """Return seconds. ``engine_seconds`` None uses the speed term only."""

    if profile.eta_factor <= 0:
        raise RoutingError(f"Profile {profile.name} has no positive ETA factor.")
    speed_seconds = distance_meters / profile_speed_mps(profile)
    if engine_seconds is None:
        base = speed_seconds
    else:
        base = max(engine_seconds, speed_seconds)
    return base * profile.eta_factor


def profile_speed_mps(profile: VehicleProfile) -> float:
    """Speed used for ETA, after the 120 mph cap."""

    if profile.max_speed_mph > 0:
        mph = min(profile.max_speed_mph, MAX_SPEED_MPH)
        speed = mph * METERS_PER_MILE / 3600
    else:
        speed = profile.max_speed_mps
        mph = speed * 3600 / METERS_PER_MILE
        if mph > MAX_SPEED_MPH:
            speed = MAX_SPEED_MPH * METERS_PER_MILE / 3600
    if speed <= 0:
        raise RoutingError(f"Profile {profile.name} has no positive max speed.")
    return speed


def cache_key(
    engine: str,
    profile: VehicleProfile,
    origin: tuple[float, float],
    destination: tuple[float, float],
) -> tuple[object, ...]:
    return (
        engine,
        profile.name,
        profile.max_speed_mps,
        profile.eta_factor,
        round(origin[0], 5),
        round(origin[1], 5),
        round(destination[0], 5),
        round(destination[1], 5),
    )


class RouteCache:
    """Small TTL cache. Keys are produced by ``cache_key``."""

    def __init__(
        self,
        max_entries: int,
        ttl_seconds: float,
        now: Callable[[], float] | None = None,
    ):
        self.max_entries = max_entries
        self.ttl_seconds = ttl_seconds
        self._now = now or time.monotonic
        self._items: dict[tuple[object, ...], tuple[float, RouteLeg]] = {}

    def get(self, key: tuple[object, ...]) -> RouteLeg | None:
        found = self._items.get(key)
        if found is None:
            return None
        stored, leg = found
        if self._now() - stored > self.ttl_seconds:
            del self._items[key]
            return None
        return replace(leg, cached=True)

    def put(self, key: tuple[object, ...], leg: RouteLeg) -> RouteLeg:
        if key not in self._items and len(self._items) >= self.max_entries:
            oldest = min(self._items, key=lambda item: self._items[item][0])
            del self._items[oldest]
        self._items[key] = (self._now(), leg)
        return leg


def route_on_msrs(
    origin: tuple[float, float],
    destination: tuple[float, float],
    msrs: Sequence[Msr],
    profile: VehicleProfile,
    snap_meters: float,
    join_meters: float,
) -> RouteLeg | None:
    """Shortest path that snaps both ends onto the MSR network."""

    nodes: list[tuple[float, float]] = []
    edges: list[tuple[int, int, float]] = []
    for route in msrs:
        if len(route.points) < 2:
            continue
        start_index = len(nodes)
        nodes.extend(route.points)
        for offset in range(len(route.points) - 1):
            left = start_index + offset
            right = left + 1
            distance = haversine_meters(
                nodes[left][0], nodes[left][1], nodes[right][0], nodes[right][1]
            )
            edges.append((left, right, distance))
    if not nodes:
        return None
    _join_nodes(nodes, edges, join_meters)
    origin_node = _snap(origin, msrs, nodes, edges, snap_meters)
    destination_node = _snap(destination, msrs, nodes, edges, snap_meters)
    if origin_node is None or destination_node is None:
        return None
    path = _dijkstra(len(nodes), edges, origin_node, destination_node)
    if path is None:
        return None
    geometry = (origin, *(nodes[index] for index in path), destination)
    distance = _path_length(geometry)
    return RouteLeg(
        engine="msr",
        distance_meters=distance,
        duration_seconds=eta_seconds(distance, profile),
        geometry=_dedupe_geometry(geometry),
        profile_name=profile.name,
        constrained_to_msr=True,
        note="Routed along imported MSR corridors. No external engine was used.",
    )


def route_document(
    leg: RouteLeg,
    origin_name: str,
    destination_name: str,
) -> dict[str, object]:
    return {
        "origin": origin_name,
        "destination": destination_name,
        "engine": leg.engine,
        "profile": leg.profile_name,
        "distance_meters": leg.distance_meters,
        "duration_seconds": leg.duration_seconds,
        "engine_duration_seconds": leg.engine_duration_seconds,
        "constrained_to_msr": leg.constrained_to_msr,
        "cached": leg.cached,
        "note": leg.note,
        "geometry": {
            "type": "LineString",
            "coordinates": [[point[1], point[0]] for point in leg.geometry],
        },
    }


def _join_nodes(
    nodes: Sequence[tuple[float, float]],
    edges: list[tuple[int, int, float]],
    join_meters: float,
) -> None:
    for left in range(len(nodes)):
        for right in range(left + 1, len(nodes)):
            distance = haversine_meters(
                nodes[left][0], nodes[left][1], nodes[right][0], nodes[right][1]
            )
            if 0 < distance <= join_meters:
                edges.append((left, right, distance))


def _snap(
    point: tuple[float, float],
    msrs: Sequence[Msr],
    nodes: list[tuple[float, float]],
    edges: list[tuple[int, int, float]],
    snap_meters: float,
) -> int | None:
    best_distance = snap_meters
    best_point: tuple[float, float] | None = None
    best_segment: tuple[int, int] | None = None
    cursor = 0
    for route in msrs:
        count = len(route.points)
        if count < 2:
            continue
        found = closest_point_on_polyline(point[0], point[1], route.points)
        if found is not None and found[2] < best_distance:
            best_distance = found[2]
            best_point = (found[0], found[1])
            best_segment = (cursor + found[3], cursor + found[3] + 1)
        cursor += count
    if best_point is None or best_segment is None:
        return None
    nodes.append(best_point)
    snapped = len(nodes) - 1
    for end in best_segment:
        edges.append(
            (
                snapped,
                end,
                haversine_meters(
                    best_point[0], best_point[1], nodes[end][0], nodes[end][1]
                ),
            )
        )
    return snapped


def _dijkstra(
    count: int,
    edges: Sequence[tuple[int, int, float]],
    start: int,
    end: int,
) -> list[int] | None:
    graph: list[list[tuple[int, float]]] = [[] for _ in range(count)]
    for left, right, weight in edges:
        graph[left].append((right, weight))
        graph[right].append((left, weight))
    distances = [float("inf")] * count
    distances[start] = 0.0
    previous = [-1] * count
    queue: list[tuple[float, int]] = [(0.0, start)]
    while queue:
        distance, node = heapq.heappop(queue)
        if distance > distances[node]:
            continue
        if node == end:
            break
        for neighbor, weight in graph[node]:
            candidate = distance + weight
            if candidate < distances[neighbor]:
                distances[neighbor] = candidate
                previous[neighbor] = node
                heapq.heappush(queue, (candidate, neighbor))
    if distances[end] == float("inf"):
        return None
    path = [end]
    cursor = end
    while cursor != start:
        cursor = previous[cursor]
        if cursor < 0:
            return None
        path.append(cursor)
    path.reverse()
    return path


def _path_length(points: Sequence[tuple[float, float]]) -> float:
    total = 0.0
    for start, end in zip(points, points[1:], strict=False):
        total += haversine_meters(start[0], start[1], end[0], end[1])
    return total


def _dedupe_geometry(
    points: tuple[tuple[float, float], ...],
) -> tuple[tuple[float, float], ...]:
    kept: list[tuple[float, float]] = []
    for point in points:
        if not kept or abs(kept[-1][0] - point[0]) > 1e-9 or abs(kept[-1][1] - point[1]) > 1e-9:
            kept.append(point)
    return tuple(kept)
