"""Basemap provider and routing engine settings."""

import os

from analytics.domain.routing import (
    MAPBOX_DIRECTIONS_BASE,
    OSRM_PUBLIC_BASE_URL,
    VehicleProfile,
    default_profiles,
)
from analytics.infrastructure.config_values import (
    _choice,
    _finite,
    _nested,
    _non_negative,
    _positive,
)
from navtracker_common.settings import env_setting, pick_setting

_MAP_PROVIDERS = {"leaflet", "mapbox", "none", "xyz", "osm"}

_ROUTING_ENGINES = {"msr", "osrm", "valhalla", "graphhopper", "mapbox"}

def _map_block(analytics: dict[str, object]) -> dict[str, object]:
    return _nested(analytics.get("map"), "analytics.map")

def _routing_block(analytics: dict[str, object]) -> dict[str, object]:
    return _nested(analytics.get("routing"), "analytics.routing")

def _map_provider(analytics: dict[str, object]) -> str:
    public = os.getenv("NEXT_PUBLIC_MAP_PROVIDER", "").strip()
    raw = public or pick_setting(
        "ANALYTICS_MAP_PROVIDER",
        _map_block(analytics).get("provider"),
        "leaflet",
    )
    chosen = _choice("NEXT_PUBLIC_MAP_PROVIDER", raw, _MAP_PROVIDERS)
    if chosen == "osm":
        return "leaflet"
    return chosen

def _map_tile_url(analytics: dict[str, object]) -> str:
    url = env_setting(
        "ANALYTICS_MAP_TILE_URL",
        str(_map_block(analytics).get("tile_url", "")),
    ).strip()
    if _map_provider(analytics) == "xyz" and not url:
        raise RuntimeError(
            "ANALYTICS_MAP_PROVIDER=xyz requires ANALYTICS_MAP_TILE_URL."
        )
    return url

def _routing_engine(analytics: dict[str, object]) -> str:
    routed = os.getenv("ROUTING_PROVIDER", "").strip()
    raw = routed or pick_setting(
        "ANALYTICS_ROUTING_ENGINE",
        _routing_block(analytics).get("engine"),
        "osrm",
    )
    return _choice("ROUTING_PROVIDER", raw, _ROUTING_ENGINES)

def _routing_base_url(analytics: dict[str, object]) -> str:
    engine = _routing_engine(analytics)
    block = _routing_block(analytics)
    yaml_key = {
        "osrm": "osrm_base_url",
        "valhalla": "valhalla_base_url",
        "graphhopper": "graphhopper_base_url",
        "mapbox": "mapbox_base_url",
    }.get(engine, "base_url")
    fallback = ""
    if engine == "osrm":
        fallback = OSRM_PUBLIC_BASE_URL
    elif engine == "mapbox":
        fallback = MAPBOX_DIRECTIONS_BASE
    configured = str(block.get(yaml_key, "") or "")
    url = env_setting("ANALYTICS_ROUTING_BASE_URL", configured or fallback).strip() or fallback
    if engine in {"valhalla", "graphhopper"} and not url:
        raise RuntimeError(
            f"ROUTING_PROVIDER={engine} requires ANALYTICS_ROUTING_BASE_URL."
        )
    if engine == "mapbox" and not _server_mapbox_token(block):
        raise RuntimeError(
            "ROUTING_PROVIDER=mapbox requires MAPBOX_SERVER_TOKEN."
        )
    return url

def _public_mapbox_token(block: dict[str, object]) -> str:
    """Client basemap token. It does not fall through to the routing token."""

    for name in ("MAPBOX_PUBLIC_TOKEN", "NEXT_PUBLIC_MAPBOX_TOKEN"):
        value = os.getenv(name, "").strip()
        if value:
            return value
    for key in ("public_token", "mapbox_token"):
        value = str(block.get(key, "") or "").strip()
        if value:
            return value
    return ""

def _server_mapbox_token(block: dict[str, object]) -> str:
    """Directions token. It is never copied from the public basemap token."""

    for name in ("MAPBOX_SERVER_TOKEN", "ANALYTICS_MAPBOX_TOKEN"):
        value = os.getenv(name, "").strip()
        if value:
            return value
    for key in ("server_token", "mapbox_token"):
        value = str(block.get(key, "") or "").strip()
        if value:
            return value
    return ""

def _routing_profiles(analytics: dict[str, object]) -> tuple[VehicleProfile, ...]:
    raw = _routing_block(analytics).get("profiles")
    if not isinstance(raw, list) or not raw:
        return default_profiles()
    profiles: list[VehicleProfile] = []
    for item in raw:
        if not isinstance(item, dict) or not str(item.get("name", "")).strip():
            raise RuntimeError("routing.profiles entries need a name.")
        name = str(item["name"]).strip()
        baseline = VehicleProfile(name=name)
        profiles.append(
            VehicleProfile(
                name=name,
                max_speed_mps=_positive(
                    "ANALYTICS_ROUTING_PROFILE_MAX_SPEED_MPS",
                    item.get("max_speed_mps", baseline.max_speed_mps),
                ),
                weight_class=str(item.get("weight_class", baseline.weight_class)),
                vehicle_type=str(item.get("vehicle_type", baseline.vehicle_type)),
                eta_factor=_positive(
                    "ANALYTICS_ROUTING_PROFILE_ETA_FACTOR",
                    item.get("eta_factor", baseline.eta_factor),
                ),
                osrm_profile=str(item.get("osrm_profile", baseline.osrm_profile)),
                valhalla_costing=str(
                    item.get("valhalla_costing", baseline.valhalla_costing)
                ),
                graphhopper_profile=str(
                    item.get("graphhopper_profile", baseline.graphhopper_profile)
                ),
            )
        )
    return tuple(profiles)

def map_settings(analytics: dict[str, object]) -> dict[str, object]:
    return {
        "map_provider": _map_provider(analytics),
        "map_tile_url": _map_tile_url(analytics),
        "map_attribution": env_setting(
                    "ANALYTICS_MAP_ATTRIBUTION",
                    str(_map_block(analytics).get("attribution", "")),
                ),
        "map_min_zoom": int(
                    _non_negative(
                        "ANALYTICS_MAP_MIN_ZOOM",
                        pick_setting(
                            "ANALYTICS_MAP_MIN_ZOOM",
                            _map_block(analytics).get("min_zoom"),
                            0,
                        ),
                    )
                ),
        "map_max_zoom": int(
                    _non_negative(
                        "ANALYTICS_MAP_MAX_ZOOM",
                        pick_setting(
                            "ANALYTICS_MAP_MAX_ZOOM",
                            _map_block(analytics).get("max_zoom"),
                            19,
                        ),
                    )
                ),
        "routing_engine": _routing_engine(analytics),
        "routing_base_url": _routing_base_url(analytics),
        "routing_cache_ttl_seconds": _non_negative(
                    "ANALYTICS_ROUTING_CACHE_TTL_SECONDS",
                    pick_setting(
                        "ANALYTICS_ROUTING_CACHE_TTL_SECONDS",
                        _routing_block(analytics).get("cache_ttl_seconds"),
                        300,
                    ),
                ),
        "routing_cache_size": int(
                    _positive(
                        "ANALYTICS_ROUTING_CACHE_SIZE",
                        pick_setting(
                            "ANALYTICS_ROUTING_CACHE_SIZE",
                            _routing_block(analytics).get("cache_size"),
                            256,
                        ),
                    )
                ),
        "routing_snap_meters": _positive(
                    "ANALYTICS_ROUTING_SNAP_METERS",
                    pick_setting(
                        "ANALYTICS_ROUTING_SNAP_METERS",
                        _routing_block(analytics).get("snap_meters"),
                        500,
                    ),
                ),
        "routing_join_meters": _non_negative(
                    "ANALYTICS_ROUTING_JOIN_METERS",
                    pick_setting(
                        "ANALYTICS_ROUTING_JOIN_METERS",
                        _routing_block(analytics).get("join_meters"),
                        40,
                    ),
                ),
        "routing_profiles": _routing_profiles(analytics),
        "routing_timeout_seconds": _positive(
                    "ANALYTICS_ROUTING_TIMEOUT_SECONDS",
                    pick_setting(
                        "ANALYTICS_ROUTING_TIMEOUT_SECONDS",
                        _routing_block(analytics).get("timeout_seconds"),
                        15,
                    ),
                ),
        "routing_mapbox_token": _server_mapbox_token(_routing_block(analytics)),
        "map_center_latitude": _finite(
                    "ANALYTICS_MAP_CENTER_LATITUDE",
                    pick_setting(
                        "ANALYTICS_MAP_CENTER_LATITUDE",
                        _map_block(analytics).get("center_latitude"),
                        39.8283,
                    ),
                ),
        "map_center_longitude": _finite(
                    "ANALYTICS_MAP_CENTER_LONGITUDE",
                    pick_setting(
                        "ANALYTICS_MAP_CENTER_LONGITUDE",
                        _map_block(analytics).get("center_longitude"),
                        -98.5795,
                    ),
                ),
        "map_zoom": int(
                    _non_negative(
                        "ANALYTICS_MAP_ZOOM",
                        pick_setting(
                            "ANALYTICS_MAP_ZOOM",
                            _map_block(analytics).get("zoom"),
                            4,
                        ),
                    )
                ),
        "map_mapbox_token": _public_mapbox_token(_map_block(analytics)),
    }
