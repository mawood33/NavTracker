"""Development replay files and the development-only HTTP routes."""

import json
from pathlib import Path

from tests.test_analytics_reports import _settings

from analytics.composition import wire_service as AnalyticsService
from analytics.infrastructure.http_api import encode_response, route_request
from analytics.infrastructure.replay_files import read_navixy_file, read_tacrep_file, tacrep_texts


def test_readers_accept_txt_json_csv_and_navixy_state_batch(tmp_path: Path):
    text = Path("fixtures/dev/tacrep_track.txt").read_text(encoding="utf-8")
    assert len(read_tacrep_file(Path("fixtures/dev/tacrep_track.txt")).splitlines()) == 5
    assert len(tacrep_texts("track.json", Path("fixtures/dev/tacrep_track.json").read_bytes())) == 2
    csv_path = tmp_path / "one.csv"
    csv_path.write_text("tacrep\n" + text.splitlines()[0] + "\n", encoding="utf-8")
    assert "1 MVR" in read_tacrep_file(csv_path)
    fixes = read_navixy_file(Path("fixtures/dev/navixy_halt.json"))
    assert [item["vehicle_id"] for item in fixes] == ["21", "21"]
    assert fixes[0]["speed_mps"] == 0
    assert fixes[1]["speed_mps"] is not None
    assert abs(float(fixes[1]["speed_mps"]) - 10.0) < 1e-9


def test_cors_header_is_added_only_for_an_allowed_origin():
    plain = encode_response(200, {"ok": True})
    assert b"Access-Control-Allow-Origin" not in plain
    allowed = encode_response(200, {"ok": True}, cors_origin="http://127.0.0.1:8081")
    assert b"Access-Control-Allow-Origin: http://127.0.0.1:8081\r\n" in allowed
    assert b"Access-Control-Allow-Headers: Authorization, Content-Type\r\n" in allowed


_DEV = {"authorization": "Bearer dev"}


def test_dev_routes_are_hidden_outside_development():
    service = AnalyticsService(_settings(environment="staging", write_token="dev"))
    status, payload, _content = route_request(
        "POST",
        "/v1/dev/fixes",
        b"{}",
        _DEV,
        service,
    )
    assert status == 404
    assert payload["error"] == "not found"
    hidden = route_request("POST", "/v1/dev/analyze", b"", _DEV, service)
    assert hidden[0] == 404


def test_dev_routes_require_the_write_token():
    service = AnalyticsService(_settings(environment="development", write_token="dev"))
    missing = route_request("POST", "/v1/dev/fixes", b"{}", {}, service)
    assert missing[0] == 401
    wrong = route_request(
        "POST",
        "/v1/dev/analyze",
        b"",
        {"authorization": "Bearer other"},
        service,
    )
    assert wrong[0] == 401


def test_dev_fixes_load_navixy_and_analyze():
    service = AnalyticsService(_settings(environment="development", write_token="dev"))
    body = Path("fixtures/dev/navixy_halt.json").read_bytes()
    status, payload, _content = route_request(
        "POST",
        "/v1/dev/fixes",
        body,
        _DEV,
        service,
    )
    assert status == 200
    assert payload["accepted"] == 2
    assert {fix.vehicle_id for fix in service.tracks()} == {"21"}
    again = route_request("POST", "/v1/dev/analyze", b"", _DEV, service)
    assert again[0] == 200
    assert again[1]["analyzed"] is True
    assert json.loads(json.dumps(payload))["accepted"] == 2
