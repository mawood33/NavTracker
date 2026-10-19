"""Routing engines. Construction does not contact the network.

OSRM, Valhalla, and GraphHopper adapters take an injected fetch
callable. Tests pass a fake. ``build_router`` installs a live fetch
only for the selected engine; nothing calls it until a route request.
"""

import json
from collections.abc import Callable, Mapping, Sequence
from typing import Protocol

import httpx

from analytics.domain.reference import Msr
from analytics.domain.routing import (
    RouteCache,
    RouteLeg,
    RoutingError,
    VehicleProfile,
    apply_profile_eta,
    cache_key,
    route_on_msrs,
)

JsonFetch = Callable[[str, Mapping[str, str], Mapping[str, object] | None], object]

OSRM_NOTE = (
    "OSRM was asked for the configured profile (default driving). "
    "Height, weight, and hazmat are not sent. "
    "ETA is the slower of the engine time and the capped vehicle speed."
)
MAPBOX_NOTE = (
    "Mapbox Directions was asked for the driving profile. "
    "Height, weight, and hazmat are not sent. "
    "ETA is the slower of the engine time and the capped vehicle speed."
)
VALHALLA_NOTE = (
    "Valhalla costing and top_speed were sent. Axle weight and height "
    "are not in the vehicle profile, so they were not sent. ETA uses the profile."
)
GRAPHHOPPER_NOTE = (
    "GraphHopper was asked for a profile name only. Truck dimensions "
    "are not sent. ETA uses the vehicle profile."
)


class RoutingEngine(Protocol):
    def route(
        self,
        origin: tuple[float, float],
        destination: tuple[float, float],
        profile: VehicleProfile,
    ) -> RouteLeg:
        """Return one leg or raise ``RoutingError``."""


class RoutingFacade:
    """Try the configured engine, then the MSR corridor fallback."""

    def __init__(
        self,
        engine: str,
        primary: RoutingEngine | None,
        cache: RouteCache,
        snap_meters: float,
        join_meters: float,
    ):
        self.engine = engine
        self.primary = primary
        self.cache = cache
        self.snap_meters = snap_meters
        self.join_meters = join_meters

    def route(
        self,
        origin: tuple[float, float],
        destination: tuple[float, float],
        profile: VehicleProfile,
        msrs: Sequence[Msr],
    ) -> RouteLeg:
        if self.primary is not None and self.engine != "msr":
            key = cache_key(self.engine, profile, origin, destination)
            cached = self.cache.get(key)
            if cached is not None:
                return cached
            try:
                produced = apply_profile_eta(
                    self.primary.route(origin, destination, profile),
                    profile,
                )
            except (RoutingError, TimeoutError, OSError, json.JSONDecodeError, httpx.HTTPError):
                produced = None
            if produced is not None:
                return self.cache.put(key, produced)
        fallback_key = cache_key("msr", profile, origin, destination)
        cached = self.cache.get(fallback_key)
        if cached is not None:
            return cached
        leg = route_on_msrs(
            origin,
            destination,
            msrs,
            profile,
            self.snap_meters,
            self.join_meters,
        )
        if leg is None:
            raise RoutingError("No MSR corridor connects these sites.")
        return self.cache.put(fallback_key, leg)


class OsrmEngine:
    def __init__(self, base_url: str, fetch: JsonFetch):
        self.base_url = base_url.rstrip("/")
        self.fetch = fetch

    def route(
        self,
        origin: tuple[float, float],
        destination: tuple[float, float],
        profile: VehicleProfile,
    ) -> RouteLeg:
        path = (
            f"/route/v1/{profile.osrm_profile}/"
            f"{origin[1]},{origin[0]};{destination[1]},{destination[0]}"
        )
        body = self.fetch(
            self.base_url + path,
            {"overview": "full", "geometries": "geojson"},
            None,
        )
        return _leg_from_geojson_route(body, "osrm", OSRM_NOTE, profile)


class ValhallaEngine:
    def __init__(self, base_url: str, fetch: JsonFetch):
        self.base_url = base_url.rstrip("/")
        self.fetch = fetch

    def route(
        self,
        origin: tuple[float, float],
        destination: tuple[float, float],
        profile: VehicleProfile,
    ) -> RouteLeg:
        payload: dict[str, object] = {
            "locations": [
                {"lat": origin[0], "lon": origin[1]},
                {"lat": destination[0], "lon": destination[1]},
            ],
            "costing": profile.valhalla_costing,
            "costing_options": {
                profile.valhalla_costing: {
                    "top_speed": int(profile.max_speed_mps * 3.6),
                }
            },
            "units": "kilometers",
        }
        body = self.fetch(self.base_url + "/route", {}, payload)
        if not isinstance(body, dict):
            raise RoutingError("Valhalla returned no route.")
        trip = body.get("trip")
        if not isinstance(trip, dict):
            raise RoutingError("Valhalla returned no route.")
        summary = trip.get("summary")
        legs = trip.get("legs")
        if not isinstance(summary, dict) or not isinstance(legs, list) or not legs:
            raise RoutingError("Valhalla returned no route.")
        length = summary.get("length")
        time_seconds = summary.get("time")
        if not isinstance(length, (int, float)) or not isinstance(time_seconds, (int, float)):
            raise RoutingError("Valhalla summary was incomplete.")
        geometry: list[tuple[float, float]] = []
        for leg in legs:
            if isinstance(leg, dict) and isinstance(leg.get("shape"), str):
                geometry.extend(decode_polyline(str(leg["shape"]), precision=6))
        if len(geometry) < 2:
            raise RoutingError("Valhalla returned no geometry.")
        return RouteLeg(
            engine="valhalla",
            distance_meters=float(length) * 1000,
            duration_seconds=float(time_seconds),
            geometry=tuple(geometry),
            profile_name=profile.name,
            constrained_to_msr=False,
            note=VALHALLA_NOTE,
        )


class GraphHopperEngine:
    def __init__(self, base_url: str, fetch: JsonFetch):
        self.base_url = base_url.rstrip("/")
        self.fetch = fetch

    def route(
        self,
        origin: tuple[float, float],
        destination: tuple[float, float],
        profile: VehicleProfile,
    ) -> RouteLeg:
        url = (
            f"{self.base_url}/route?point={origin[0]},{origin[1]}"
            f"&point={destination[0]},{destination[1]}"
            f"&profile={profile.graphhopper_profile}&points_encoded=false"
        )
        body = self.fetch(url, {}, None)
        if not isinstance(body, dict):
            raise RoutingError("GraphHopper returned no route.")
        paths = body.get("paths")
        if not isinstance(paths, list) or not paths or not isinstance(paths[0], dict):
            raise RoutingError("GraphHopper returned no route.")
        path = paths[0]
        distance = path.get("distance")
        time_ms = path.get("time")
        points = path.get("points")
        if (
            not isinstance(distance, (int, float))
            or not isinstance(time_ms, (int, float))
            or not isinstance(points, dict)
            or not isinstance(points.get("coordinates"), list)
        ):
            raise RoutingError("GraphHopper path was incomplete.")
        geometry: list[tuple[float, float]] = []
        for pair in points["coordinates"]:
            if isinstance(pair, list) and len(pair) >= 2:
                geometry.append((float(pair[1]), float(pair[0])))
        if len(geometry) < 2:
            raise RoutingError("GraphHopper returned no geometry.")
        return RouteLeg(
            engine="graphhopper",
            distance_meters=float(distance),
            duration_seconds=float(time_ms) / 1000,
            geometry=tuple(geometry),
            profile_name=profile.name,
            constrained_to_msr=False,
            note=GRAPHHOPPER_NOTE,
        )


def build_router(
    engine: str,
    base_url: str,
    cache: RouteCache,
    snap_meters: float,
    join_meters: float,
    mapbox_token: str = "",
    timeout_seconds: float = 15,
) -> RoutingFacade:
    def fetch(
        url: str,
        params: Mapping[str, str],
        payload: Mapping[str, object] | None,
    ) -> object:
        return _live_fetch(url, params, payload, timeout_seconds)

    primary: RoutingEngine | None
    if engine == "msr":
        primary = None
    elif engine == "osrm":
        primary = OsrmEngine(base_url, fetch)
    elif engine == "valhalla":
        primary = ValhallaEngine(base_url, fetch)
    elif engine == "graphhopper":
        primary = GraphHopperEngine(base_url, fetch)
    elif engine == "mapbox":
        primary = MapboxEngine(base_url, mapbox_token, fetch)
    else:
        raise RoutingError(f"Unknown routing engine {engine}.")
    return RoutingFacade(engine, primary, cache, snap_meters, join_meters)


class MapboxEngine:
    """Mapbox Directions. The token is a request parameter, not a log line."""

    def __init__(self, base_url: str, token: str, fetch: JsonFetch):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.fetch = fetch

    def route(
        self,
        origin: tuple[float, float],
        destination: tuple[float, float],
        profile: VehicleProfile,
    ) -> RouteLeg:
        if not self.token.strip():
            raise RoutingError("Mapbox access token is required.", "mapbox_token")
        profile_name = profile.mapbox_profile or "driving"
        path = (
            f"/directions/v5/mapbox/{profile_name}/"
            f"{origin[1]},{origin[0]};{destination[1]},{destination[0]}"
        )
        body = self.fetch(
            self.base_url + path,
            {
                "geometries": "geojson",
                "overview": "full",
                "access_token": self.token,
            },
            None,
        )
        return _leg_from_geojson_route(body, "mapbox", MAPBOX_NOTE, profile)


def _leg_from_geojson_route(
    body: object,
    engine: str,
    note: str,
    profile: VehicleProfile,
) -> RouteLeg:
    if not isinstance(body, dict):
        raise RoutingError(f"{engine} returned no route.")
    routes = body.get("routes")
    if not isinstance(routes, list) or not routes or not isinstance(routes[0], dict):
        raise RoutingError(f"{engine} returned no route.")
    route = routes[0]
    distance = route.get("distance")
    duration = route.get("duration")
    geometry = route.get("geometry")
    if (
        not isinstance(distance, (int, float))
        or not isinstance(duration, (int, float))
        or not isinstance(geometry, dict)
    ):
        raise RoutingError(f"{engine} route was incomplete.")
    coordinates = geometry.get("coordinates")
    if not isinstance(coordinates, list) or len(coordinates) < 2:
        raise RoutingError(f"{engine} returned no geometry.")
    points: list[tuple[float, float]] = []
    for pair in coordinates:
        if isinstance(pair, list) and len(pair) >= 2:
            points.append((float(pair[1]), float(pair[0])))
    return RouteLeg(
        engine=engine,
        distance_meters=float(distance),
        duration_seconds=float(duration),
        geometry=tuple(points),
        profile_name=profile.name,
        constrained_to_msr=False,
        note=note,
    )


def decode_polyline(encoded: str, precision: int = 6) -> list[tuple[float, float]]:
    """Decode a Valhalla or Google polyline into latitude, longitude pairs."""

    factor = 10**precision
    points: list[tuple[float, float]] = []
    index = 0
    latitude = 0
    longitude = 0
    while index < len(encoded):
        index, latitude_delta = _polyline_value(encoded, index)
        index, longitude_delta = _polyline_value(encoded, index)
        latitude += latitude_delta
        longitude += longitude_delta
        points.append((latitude / factor, longitude / factor))
    return points


def _polyline_value(encoded: str, index: int) -> tuple[int, int]:
    result = 0
    shift = 0
    while index < len(encoded):
        chunk = ord(encoded[index]) - 63
        index += 1
        result |= (chunk & 0x1F) << shift
        shift += 5
        if chunk < 0x20:
            break
    delta = ~(result >> 1) if result & 1 else result >> 1
    return index, delta


def _live_fetch(
    url: str,
    params: Mapping[str, str],
    payload: Mapping[str, object] | None,
    timeout_seconds: float = 15,
) -> object:
    timeout = timeout_seconds
    try:
        if payload is not None:
            response = httpx.post(url, json=dict(payload), timeout=timeout)
        elif params:
            response = httpx.get(url, params=dict(params), timeout=timeout)
        else:
            response = httpx.get(url, timeout=timeout)
        if response.status_code >= 400:
            raise RoutingError("Routing engine request failed.")
        body = response.json()
    except RoutingError:
        raise
    except (httpx.HTTPError, json.JSONDecodeError, TimeoutError, OSError) as exc:
        raise RoutingError("Routing engine request failed.") from exc
    if not isinstance(body, dict):
        raise RoutingError("Routing engine returned an unexpected body.")
    return body
