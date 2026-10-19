"""Schema 2 documents shared by HTTP, WebSocket, OGC, and the push adapter."""

import asyncio
import json
from datetime import UTC, datetime, timedelta

from tests.test_analytics_reports import _settings

from analytics.composition import wire_service as AnalyticsService
from analytics.domain.access import AuthPolicy
from analytics.domain.grid import CellActivity
from analytics.domain.outputs import SCHEMA_VERSION, diff_documents
from analytics.domain.reference import Msr, Site
from analytics.domain.tracks import GpsFix
from analytics.infrastructure.frontend import FrontendPublisher
from analytics.infrastructure.http_api import route_request

T0 = datetime(2026, 1, 1, tzinfo=UTC)


def _service() -> AnalyticsService:
    service = AnalyticsService(_settings(max_fixes_per_vehicle=100), None)
    service._sites = (
        Site(
            name="A",
            site_types=frozenset({"launch"}),
            status="confirmed",
            confidence=0.9,
            activity_status="active",
            latitude=10.0,
            longitude=20.0,
            moa="06",
        ),
    )
    service._cells = (
        CellActivity(
            cell_id="hex",
            latitude=10.0,
            longitude=20.0,
            boundary=((10.0, 20.0), (10.1, 20.0), (10.1, 20.1)),
            score=2.0,
            stops=1,
            dwell_seconds=0.0,
            vehicle_ids=("truck",),
            revisits=0,
            milling=False,
            on_msr=True,
            long_dwell=False,
            hotspot=True,
        ),
    )
    service._msrs = (Msr(name="RED", points=((10.0, 20.0), (11.0, 21.0))),)
    service.add_fixes(
        [
            GpsFix("truck", 10.0, 20.0, T0, speed_mps=3.0, heading=90.0, origin_id="m1"),
            GpsFix(
                "truck",
                10.2,
                20.2,
                T0 + timedelta(seconds=30),
                speed_mps=4.0,
                heading=80.0,
                origin_id="m2",
            ),
        ]
    )
    return service


def _core(site: dict[str, object]) -> dict[str, object]:
    return {
        "status": site["status"],
        "assessment": site["assessment"],
        "confidence": site["confidence"],
        "types": site["types"],
        "confidence_band": site["confidence_band"],
    }


def test_site_fields_match_on_http_websocket_and_ogc():
    service = _service()
    expected = {
        "status": "active",
        "assessment": "confirmed",
        "confidence": 0.9,
        "types": ["launch"],
        "confidence_band": "confirmed",
    }
    snapshot = service.output_snapshot(T0)
    assert snapshot["schema"] == "navtracker.analytics"
    assert snapshot["schema_version"] == SCHEMA_VERSION == 2
    assert _core(snapshot["sites"][0]) == expected
    assert snapshot["cells"][0]["boundary"][0] == [10.0, 20.0]
    assert snapshot["routes"][0]["points"] == [[10.0, 20.0], [11.0, 21.0]]
    assert snapshot["tracks"][0]["origin_id"] == "m1"
    assert snapshot["tracks"][0]["lat"] == 10.0
    assert snapshot["tracks"][0]["speed"] == 3.0
    listed = route_request("GET", "/sites", b"", {}, service)[1]
    api = route_request("GET", "/api/sites", b"", {}, service)[1]
    assert _core(listed["sites"][0]) == expected
    assert _core(api["sites"][0]) == expected
    features = route_request("GET", "/ogc/collections/sites/items", b"", {}, service)[1]
    assert _core(features["features"][0]["properties"]) == expected
    tracks = route_request("GET", "/api/tracks", b"", {}, service)[1]
    assert tracks == {"tracks": snapshot["tracks"]}
    assert route_request("GET", "/tracks", b"", {}, service)[1] == tracks


def test_diffs_remove_sites_cells_routes_and_tracks():
    service = _service()
    first = service.publish_output()
    assert first is not None and first["type"] == "snapshot"
    queue = service.hub.subscribe()
    service._ingest.retract("m1")
    event = queue.get_nowait()
    assert event["type"] == "retract"
    assert event["schema_version"] == 2
    assert event["tracks"][0]["origin_id"] == "m1"
    stored = service.hub.latest_snapshot()
    assert stored is not None
    assert [item["origin_id"] for item in stored["tracks"]] == ["m2"]
    service._sites = ()
    service._cells = ()
    service._msrs = ()
    update = service.publish_output()
    assert update is not None
    removed = {
        (change["op"], change["kind"], change.get("name") or change.get("cell_id"))
        for change in update["changes"]
        if change["op"] == "remove"
    }
    assert ("remove", "site", "A") in removed
    assert ("remove", "cell", "hex") in removed
    assert ("remove", "route", "RED") in removed
    track_removes = [
        change
        for change in update["changes"]
        if change["kind"] == "track" and change["op"] == "remove"
    ]
    assert track_removes[0]["origin_id"] == "m1"
    assert track_removes[0]["vehicle_id"] == "truck"


def test_per_vehicle_cap_keeps_the_newest_fix():
    service = AnalyticsService(_settings(max_fixes_per_vehicle=1), None)
    service.add_fixes(
        [
            GpsFix("truck", 1, 2, T0, origin_id="old"),
            GpsFix("truck", 1, 2, T0 + timedelta(seconds=5), origin_id="new"),
            GpsFix("other", 3, 4, T0, origin_id="kept"),
        ]
    )
    assert sorted(fix.origin_id for fix in service._fixes) == ["kept", "new"]
    rows = service.output_snapshot(T0)["tracks"]
    assert [row["origin_id"] for row in rows] == ["kept", "new"]


def test_tracks_require_the_read_token_and_ogc_filters_time():
    service = _service()
    policy = AuthPolicy(read_token="read", write_token="write")
    assert route_request("GET", "/tracks", b"", {}, service, auth=policy)[0] == 401
    assert route_request("GET", "/api/tracks", b"", {}, service, auth=policy)[0] == 401
    denied = route_request(
        "GET",
        "/ogc/collections/tracks/items",
        b"",
        {},
        service,
        auth=policy,
    )
    assert denied[0] == 401
    headers = {"authorization": "Bearer read"}
    allowed = route_request("GET", "/tracks", b"", headers, service, auth=policy)
    assert allowed[0] == 200
    assert len(allowed[1]["tracks"]) == 2
    window = route_request(
        "GET",
        "/ogc/collections/tracks/items?datetime=2026-01-01T00:00:20Z/2026-01-01T00:01:00Z",
        b"",
        headers,
        service,
        auth=policy,
    )
    assert window[0] == 200
    assert len(window[1]["features"]) == 1
    assert window[1]["features"][0]["properties"]["origin_id"] == "m2"
    assert window[1]["features"][0]["geometry"]["coordinates"] == [20.2, 10.2]


def test_direct_diff_emits_remove_when_a_site_disappears():
    previous = {
        "sites": [
            {
                "name": "A",
                "status": "active",
                "assessment": "possible",
                "confidence": 0.2,
                "types": [],
            }
        ]
    }
    current = {
        "as_of": "2026-01-01T00:00:00+00:00",
        "basemap": {"provider": "none"},
        "routing": {"engine": "msr"},
        "sites": [],
        "cells": [],
        "routes": [],
        "tracks": [],
    }
    update = diff_documents(previous, current)
    assert update is not None
    assert update["schema_version"] == 2
    assert update["changes"] == [{"op": "remove", "kind": "site", "name": "A"}]
    assert diff_documents(current, current) is None


def test_push_adapter_receives_the_schema_2_snapshot():
    service = _service()
    document = service.publish_output()
    assert document is not None
    sent: list[bytes] = []

    async def post(_url: str, body: bytes) -> None:
        sent.append(body)

    publisher = FrontendPublisher("http://frontend.example/ingest", post=post)
    asyncio.run(publisher.publish(document))
    posted = json.loads(sent[0])
    assert posted["schema_version"] == 2
    assert posted["sites"][0]["assessment"] == "confirmed"
    assert posted["sites"][0]["assessed_level"] == "confirmed"
    assert posted["sites"][0]["basis"] == "single_source"
    assert posted["sites"][0]["disagreement"] is False
    assert posted["sites"][0]["status"] == "active"
    assert posted["tracks"][0]["vehicle_id"] == "truck"
