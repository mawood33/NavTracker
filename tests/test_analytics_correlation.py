import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import quote

import pytest
from tests.test_analytics_reports import _settings

from analytics.application.service import AnalyticsService
from analytics.config_validation import validate_analytics_environment
from analytics.domain.assessment import ConfidencePolicy, SiteActivity, assess_site
from analytics.domain.correlation import (
    ACTIVITY_STATES,
    CorrelationPolicy,
    TacrepMessage,
    advance_for_gap,
    correlate,
    correlation_evidence,
    dedupe_messages,
    manual_advance,
    pair_score,
    parse_tacrep_line,
    replay_activity,
)
from analytics.domain.outputs import MAPBOX_STYLE, basemap_document
from analytics.domain.reference import Evidence, Site
from analytics.domain.routing import (
    MAX_SPEED_MPH,
    METERS_PER_MILE,
    RoutingError,
    VehicleProfile,
    limit_speed,
)
from analytics.infrastructure.config import load_analytics_settings
from analytics.infrastructure.http_api import route_request
from analytics.infrastructure.routing_engines import MapboxEngine

T0 = datetime(2026, 1, 1, tzinfo=UTC)
LINE = (
    "O-7//2026-01-01T00:00:00Z//10.5,-80.25//subject=alpha//id=m9"
    "//speed=12//heading=90//length=4//width=2//confidence=0.4//bmoa=AREA"
)


def test_tacrep_line_fields_and_dedupe_key():
    parsed = parse_tacrep_line(LINE, source_id="room-a", fallback_message_id="fallback")
    assert parsed is not None
    assert parsed.kind == "O"
    assert parsed.sequence == "7"
    assert parsed.latitude == 10.5
    assert parsed.longitude == -80.25
    assert parsed.subject == "alpha"
    assert parsed.source_message_id != "m9"
    assert len(parsed.source_message_id) == 32
    assert parsed.speed_mps == 12
    assert parsed.bmoa == "AREA"
    duplicate = TacrepMessage(
        kind="O",
        sequence="8",
        observed_at=T0,
        latitude=1,
        longitude=2,
        source_id="room-a",
        source_message_id=parsed.source_message_id,
    )
    other = TacrepMessage(
        kind="J",
        sequence="1",
        observed_at=T0,
        latitude=1,
        longitude=2,
        source_id="room-b",
        source_message_id="m9",
    )
    kept = dedupe_messages((parsed, duplicate, other))
    assert len(kept) == 2
    assert kept[0].sequence == "7"


def test_pair_score_weights_and_unused_speed():
    policy = CorrelationPolicy()
    assert policy.minimum_transload_speed_mps == 0.5
    left = _message("J", 0, subject="same", bmoa="AREA", msr="MSR RED", speed_mps=0)
    right = _message("I", 0, subject="same", bmoa="AREA", msr="MSR RED", speed_mps=40)
    same_cell = pair_score(left, right, policy, same_cell=True)
    assert abs(same_cell - 1.0) < 1e-9
    shared_bmoa = pair_score(
        _message("J", 0, subject="same", bmoa="AREA", cell_id="a"),
        _message("I", 0, subject="same", bmoa="AREA", cell_id="b"),
        policy,
        same_cell=False,
    )
    assert abs(shared_bmoa - (0.45 + 0.35 * 0.8 + 0.20)) < 1e-9
    shared_msr = pair_score(
        _message("J", 0, subject="same", msr="MSR RED", cell_id="a"),
        _message("I", 0, subject="same", msr="MSR RED", cell_id="b"),
        policy,
        same_cell=False,
    )
    assert abs(shared_msr - (0.45 + 0.35 * 0.6 + 0.20)) < 1e-9
    half = pair_score(left, _message("I", 22.5, subject="same"), policy, same_cell=True)
    assert abs(half - (0.45 + 0.35 + 0.10)) < 1e-9
    fast = pair_score(left, right, policy, same_cell=True)
    assert fast == same_cell


def test_probable_requires_three_sources_and_the_score_floor():
    close = [
        _message("O", 0, source_id="a", cell_id="cell", subject="unit"),
        _message("O", 1, source_id="b", cell_id="cell", subject="unit"),
        _message("J", 2, source_id="c", cell_id="cell", subject="unit"),
    ]
    probable = correlate(close)[0]
    assert probable.label == "probable"
    assert probable.best_score >= 0.70
    possible = correlate(close[:2])[0]
    assert possible.label == "possible"
    distant = [
        _message("J", 0, source_id="a", cell_id="cell", subject="one"),
        _message("I", 44, source_id="b", cell_id="cell", subject="two"),
        _message("E", 44, source_id="c", cell_id="cell", subject="three"),
    ]
    assert correlate(distant)[0].label == "possible"


def test_activity_states_follow_launch_gap_and_shared_bmoa():
    opened = replay_activity(
        (_message("O", 0, bmoa="AREA"),),
        cell_id="cell",
        bmoa="AREA",
        hide_gap_minutes=20,
    )
    assert opened.state == "launch"
    assert opened.acceptance == 1.0
    hidden = advance_for_gap(opened, T0 + timedelta(minutes=21), 20)
    assert hidden.state == "hide_after_launch"
    assert hidden.acceptance == 1.0
    moved = replay_activity(
        (
            _message("O", 0, bmoa="AREA"),
            _message("J", 21, bmoa="AREA"),
        ),
        cell_id="cell",
        bmoa="AREA",
        hide_gap_minutes=20,
    )
    assert moved.state == "transload"
    assert moved.acceptance == 1.0
    rejected = replay_activity(
        (
            _message("O", 0, bmoa="AREA"),
            _message("J", 21, bmoa="OTHER"),
        ),
        cell_id="cell",
        bmoa="AREA",
        hide_gap_minutes=20,
    )
    assert rejected.state == "hide_after_launch"
    assert rejected.acceptance == 0.0
    imagery = replay_activity(
        (
            _message("O", 0, bmoa="AREA"),
            _message("I", 21, bmoa="AREA"),
            _message("E", 22, bmoa="AREA"),
        ),
        cell_id="cell",
        bmoa="AREA",
        hide_gap_minutes=20,
    )
    assert imagery.state == "transload"
    end = hidden
    for _ in ACTIVITY_STATES:
        end = manual_advance(end, T0 + timedelta(hours=2))
    assert end.state == "hide_after_transload"
    assert "closed" not in ACTIVITY_STATES


def test_verified_alias_and_scaled_acceptance():
    site = Site(name="06001", latitude=10.0, longitude=20.0, status="possible", confidence=0.2)
    accepted = correlate(
        (
            _message("O", 0, source_id="a", cell_id="cell", subject="unit"),
            _message("O", 1, source_id="b", cell_id="cell", subject="unit"),
            _message("O", 2, source_id="c", cell_id="cell", subject="unit"),
        )
    )[0]

    activity = SiteActivity(
        as_of=T0,
        stops=0,
        dwell_seconds=0,
        distinct_vehicles=0,
        revisits=0,
        milling=False,
        on_msr=False,
        long_dwell=False,
        cell_id="cell",
        cell_score=0,
        cell_hotspot=False,
        reports=(),
        last_activity_at=T0,
        pending_evidence=correlation_evidence(accepted),
        correlation_probable=True,
    )
    assessed = assess_site(site, activity, ConfidencePolicy())
    assert "launch" in assessed.site_types
    assert assessed.status == "probable"
    assert assessed.confidence < 0.8
    truth = assess_site(
        Site(
            name="06001",
            latitude=10,
            longitude=20,
            evidence=assessed.evidence
            + (Evidence(kind="truth", weight=0.8, detail="manual", observed_at=T0),),
        ),
        activity,
        ConfidencePolicy(),
    )
    assert truth.status == "confirmed"
    assert truth.verified is True


def test_speed_cap_and_mapbox_token_stay_out_of_errors():
    profile = limit_speed(VehicleProfile(name="truck", max_speed_mps=18), 500)
    assert profile.max_speed_mph == MAX_SPEED_MPH
    with pytest.raises(RoutingError) as capped:
        limit_speed(profile, 0)
    assert capped.value.code == "invalid_speed"
    seen: dict[str, str] = {}

    def fetch(url, params, payload):
        seen["token"] = str(params.get("access_token", ""))
        raise RoutingError("down")

    engine = MapboxEngine("https://api.mapbox.com", "", fetch)
    with pytest.raises(RoutingError) as missing:
        engine.route((0, 0), (1, 1), profile)
    assert missing.value.code == "mapbox_token"
    assert "token" not in seen
    engine.token = "pk.test-token"
    with pytest.raises(RoutingError) as failed:
        engine.route((0, 0), (1, 1), profile)
    assert seen["token"] == "pk.test-token"
    assert "pk.test-token" not in str(failed.value)
    dark = basemap_document("mapbox", "", "", 0, 19, mapbox_token="pk.test-token")
    assert dark["style"] == MAPBOX_STYLE
    assert dark["mapbox_token_configured"] is True
    assert "pk.test-token" not in json.dumps({k: v for k, v in dark.items() if k != "tile_url"})


def test_transload_oracle_http_contract(tmp_path: Path):
    reference = tmp_path / "reference"
    reference.mkdir()
    (reference / "sites.kml").write_text(
        """<?xml version="1.0"?>
        <kml xmlns="http://www.opengis.net/kml/2.2"><Document>
          <Placemark><name>A</name><Point><coordinates>0,0,0</coordinates></Point></Placemark>
          <Placemark><name>B</name><Point><coordinates>0,0.01,0</coordinates></Point></Placemark>
          <Placemark><name>MSR RED</name><LineString>
            <coordinates>0,0,0 0,0.01,0</coordinates>
          </LineString></Placemark>
        </Document></kml>
        """,
        encoding="utf-8",
    )
    service = AnalyticsService(_settings(reference_dir=str(reference)), None)
    service.load_reference()
    rail = route_request(
        "POST",
        "/api/routes",
        json.dumps(
            {"originSiteId": "A", "destinationSiteId": "B", "vehicleType": "rail"}
        ).encode(),
        {},
        service,
    )
    assert rail[0] == 400
    assert rail[1]["code"] == "rail_unsupported"
    missing = route_request(
        "POST",
        "/api/routes",
        json.dumps(
            {"originSiteId": "A", "destinationSiteId": "missing", "vehicleType": "truck"}
        ).encode(),
        {},
        service,
    )
    assert missing[0] == 400
    assert missing[1]["code"] == "missing_site"
    routed = route_request(
        "POST",
        "/api/routes",
        json.dumps(
            {
                "originSiteId": "A",
                "destinationSiteId": "B",
                "vehicleType": "intermodal",
                "maxSpeedMph": 500,
            }
        ).encode(),
        {},
        service,
    )
    assert routed[0] == 200
    body = routed[1]
    assert body["provider"] == "msr"
    leg = body["routes"][0]
    assert leg["vehicleType"] == "intermodal"
    speed = MAX_SPEED_MPH * METERS_PER_MILE / 3600
    assert abs(leg["durationMinutes"] - leg["distanceMeters"] / speed / 60) < 1e-6
    fresh = datetime.now(UTC).replace(microsecond=0).isoformat()
    tacrep = f"O-7//{fresh}//10.5,-80.25//subject=alpha//id=m9//bmoa=AREA"
    status, payload, _content = route_request(
        "POST",
        "/v1/events/tacrep",
        tacrep.encode(),
        {},
        service,
    )
    assert status == 200
    assert payload["accepted"] == 1
    correlations = route_request("GET", "/v1/events/correlations", b"", {}, service)[1]
    assert correlations["correlations"][0]["label"] == "possible"
    activities = route_request("GET", "/v1/activities", b"", {}, service)[1]
    activity_id = activities["activities"][0]["id"]
    assert activities["activities"][0]["state"] == "launch"
    advanced = route_request(
        "POST",
        "/v1/activities/" + quote(activity_id, safe="") + "/advance",
        b"",
        {},
        service,
    )
    assert advanced[0] == 200
    assert advanced[1]["state"] == "hide_after_launch"
    stream = route_request("GET", "/v1/activities/stream", b"", {}, service)
    assert stream[2] == "text/event-stream"
    assert str(stream[1]).startswith("data: ")
    verified = route_request("POST", "/api/sites/A/verify", b"", {}, service)
    assert verified[0] == 200
    assert verified[1]["assessment"] == "verified"
    assert verified[1]["status"] == "unknown"
    listed = route_request("GET", "/api/sites", b"", {}, service)[1]
    names = {site["id"] for site in listed["sites"]}
    assert "A" in names


def test_checked_in_defaults_match_transload_oracle():
    settings = validate_analytics_environment("development")
    assert settings.grid.resolution == 9
    assert settings.map_provider == "leaflet"
    assert settings.map_center_latitude == 39.8283
    assert settings.map_center_longitude == -98.5795
    assert settings.map_zoom == 4
    assert settings.routing_engine == "osrm"
    assert settings.routing_base_url == "https://router.project-osrm.org"
    assert settings.routing_timeout_seconds == 15
    assert settings.correlation.window_minutes == 45
    assert settings.correlation.hide_gap_minutes == 20
    assert settings.correlation.minimum_correlation_score == 0.70
    assert settings.correlation.minimum_sources == 3
    assert settings.http_host == "127.0.0.1"
    assert settings.coord_card_path == ""
    staging = validate_analytics_environment("staging")
    production = validate_analytics_environment("production")
    assert staging.map_provider == "none"
    assert staging.routing_engine == "msr"
    assert production.map_provider == "none"
    assert production.routing_engine == "msr"
    assert production.http_host == "127.0.0.1"


def test_mapbox_settings_require_a_token(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("CHAT_SURFER_API_KEY", "dummy")
    monkeypatch.delenv("NEXT_PUBLIC_MAPBOX_TOKEN", raising=False)
    monkeypatch.delenv("ANALYTICS_MAPBOX_TOKEN", raising=False)
    monkeypatch.setenv("NEXT_PUBLIC_MAP_PROVIDER", "mapbox")
    with pytest.raises(RuntimeError, match="MAPBOX_TOKEN"):
        load_analytics_settings()


def _message(
    kind: str,
    minute: float,
    *,
    source_id: str = "a",
    subject: str = "unit",
    bmoa: str = "",
    msr: str = "",
    cell_id: str = "cell",
    speed_mps: float | None = None,
) -> TacrepMessage:
    return TacrepMessage(
        kind=kind,
        sequence="1",
        observed_at=T0 + timedelta(minutes=minute),
        latitude=10.0,
        longitude=20.0,
        source_id=source_id,
        source_message_id=f"{source_id}-{kind}-{minute}",
        subject=subject,
        speed_mps=speed_mps,
        bmoa=bmoa,
        msr=msr,
        cell_id=cell_id,
    )
