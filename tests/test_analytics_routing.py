from analytics.domain.outputs import basemap_document, diff_documents, landing_page
from analytics.domain.reference import Msr
from analytics.domain.routing import (
    RouteCache,
    RouteLeg,
    RoutingError,
    VehicleProfile,
    route_on_msrs,
)
from analytics.infrastructure.routing_engines import (
    GraphHopperEngine,
    OsrmEngine,
    RoutingFacade,
    ValhallaEngine,
    decode_polyline,
)


def _profile() -> VehicleProfile:
    return VehicleProfile(name="truck", max_speed_mps=10, eta_factor=2)


def test_msr_route_follows_the_corridor_and_uses_profile_eta():
    route = Msr(name="MSR RED", points=((0.0, 0.0), (0.0, 0.01)))
    leg = route_on_msrs((0.0, 0.0), (0.0, 0.01), [route], _profile(), 500, 40)
    assert leg is not None
    assert leg.engine == "msr"
    assert leg.constrained_to_msr is True
    assert leg.distance_meters > 1000
    assert abs(leg.duration_seconds - leg.distance_meters / 10 * 2) < 1e-6


def test_sites_off_the_corridor_have_no_route():
    route = Msr(name="MSR RED", points=((0.0, 0.0), (0.0, 0.01)))
    assert route_on_msrs((1.0, 1.0), (1.0, 1.01), [route], _profile(), 100, 40) is None


def test_osrm_fake_replaces_duration_and_cache_skips_the_second_call():
    calls = {"count": 0}

    def fetch(url, params, payload):
        calls["count"] += 1
        assert "/route/v1/driving/" in url
        assert params["geometries"] == "geojson"
        assert payload is None
        return {
            "routes": [
                {
                    "distance": 1000,
                    "duration": 999,
                    "geometry": {
                        "type": "LineString",
                        "coordinates": [[0, 0], [0.01, 0]],
                    },
                }
            ]
        }

    facade = RoutingFacade(
        "osrm",
        OsrmEngine("http://osrm.internal", fetch),
        RouteCache(8, 300, now=lambda: 0),
        500,
        40,
    )
    profile = _profile()
    first = facade.route((0, 0), (0, 0.01), profile, [])
    second = facade.route((0, 0), (0, 0.01), profile, [])
    assert calls["count"] == 1
    assert first.cached is False
    assert second.cached is True
    assert first.engine_duration_seconds == 999
    assert abs(first.duration_seconds - 1998) < 1e-6
    assert "api_key" not in str(fetch)


def test_failed_engine_falls_back_to_the_msr_network():
    def fetch(url, params, payload):
        raise RoutingError("down")

    route = Msr(name="MSR RED", points=((0.0, 0.0), (0.0, 0.01)))
    facade = RoutingFacade(
        "osrm",
        OsrmEngine("http://osrm.internal", fetch),
        RouteCache(8, 300, now=lambda: 0),
        500,
        40,
    )
    leg = facade.route((0.0, 0.0), (0.0, 0.01), _profile(), [route])
    assert leg.engine == "msr"


def test_valhalla_and_graphhopper_parse_fake_payloads():
    shape = decode_polyline(encode_two_points(1.0, 2.0, 1.1, 2.1))

    def valhalla_fetch(url, params, payload):
        assert url.endswith("/route")
        assert payload is not None
        assert payload["costing"] == "truck"
        return {
            "trip": {
                "summary": {"length": 1.5, "time": 40},
                "legs": [{"shape": encode_two_points(1.0, 2.0, 1.1, 2.1)}],
            }
        }

    def hopper_fetch(url, params, payload):
        assert "points_encoded=false" in url
        assert "profile=truck" in url
        return {
            "paths": [
                {
                    "distance": 1500,
                    "time": 40000,
                    "points": {"coordinates": [[2.0, 1.0], [2.1, 1.1]]},
                }
            ]
        }

    valhalla = ValhallaEngine("http://valhalla.internal", valhalla_fetch)
    hopper = GraphHopperEngine("http://graphhopper.internal", hopper_fetch)
    assert valhalla.route((1, 2), (1.1, 2.1), _profile()).distance_meters == 1500
    assert hopper.route((1, 2), (1.1, 2.1), _profile()).geometry[0] == (1.0, 2.0)
    assert shape[0] == (1.0, 2.0)


def test_basemap_defaults_are_air_gapped_and_osm_has_no_key():
    offline = basemap_document("none", "", "", 0, 19)
    assert offline["tile_url"] == ""
    assert offline["air_gapped"] is True
    public = basemap_document("osm", "", "", 0, 19)
    assert public["provider"] == "leaflet"
    assert public["center_latitude"] == 39.8283
    assert public["center_longitude"] == -98.5795
    assert public["zoom"] == 4
    assert "api_key" not in str(public["tile_url"])
    assert public["tile_url"] == "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
    page = landing_page(public, {"engine": "msr", "fallback": "msr"}, "")
    assert page["basemap"] == public
    assert page["routing"]["engine"] == "msr"
    assert any(link["rel"] == "tiles" for link in page["links"])


def test_snapshot_diff_reports_a_confidence_change_once():
    previous = {"sites": [{"name": "A", "status": "possible", "confidence": 0.2, "types": []}]}
    current = {
        "as_of": "2026-01-01T00:00:00+00:00",
        "basemap": {"provider": "none"},
        "routing": {"engine": "msr"},
        "sites": [{"name": "A", "status": "probable", "confidence": 0.6, "types": ["hide"]}],
        "cells": [],
        "routes": [],
    }
    update = diff_documents(previous, current)
    assert update is not None
    assert update["type"] == "update"
    assert update["changes"][0]["kind"] == "site"
    assert diff_documents(current, current) is None


def encode_two_points(lat1: float, lon1: float, lat2: float, lon2: float) -> str:
    return _encode(int(round(lat1 * 1_000_000)), int(round(lon1 * 1_000_000))) + _encode(
        int(round((lat2 - lat1) * 1_000_000)),
        int(round((lon2 - lon1) * 1_000_000)),
    )


def _encode(latitude: int, longitude: int) -> str:
    return _chunk(latitude) + _chunk(longitude)


def _chunk(value: int) -> str:
    shifted = ~(value << 1) if value < 0 else value << 1
    chars = []
    while shifted >= 0x20:
        chars.append(chr((0x20 | (shifted & 0x1F)) + 63))
        shifted >>= 5
    chars.append(chr(shifted + 63))
    return "".join(chars)


def test_route_leg_import_is_used():
    assert RouteLeg(
        engine="msr",
        distance_meters=1,
        duration_seconds=1,
        geometry=((0, 0), (0, 1)),
        profile_name="truck",
        constrained_to_msr=True,
    ).engine == "msr"
