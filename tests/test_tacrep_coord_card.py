import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from tests.test_analytics_reports import _settings

from analytics.composition import wire_service as AnalyticsService
from analytics.domain.access import AuthPolicy
from analytics.domain.coordcard import CoordCardError, parse_coord_card_text
from analytics.domain.correlation import (
    CorrelationPolicy,
    TacrepBlock,
    correlate,
    correlation_evidence,
    dedupe_messages,
)
from analytics.domain.tacrep_parse import parse_tacrep_body
from analytics.domain.tracks import GpsFix
from analytics.infrastructure.http_api import route_request
from analytics.infrastructure.mgrs_grid import MgrsLibrary

T0 = datetime(2026, 1, 1, tzinfo=UTC)
CARD = """
mismatch_meters: 100
types:
  - name: IMINT
    kind: I
    start: 100
    end: 199
    fields: [callsign, id, mgrs, latlon, azimuth, speed, event, remarks]
  - name: MTI
    kind: J
    start: 400
    end: 499
    fields: [callsign, id, mgrs, latlon, azimuth, speed, event]
"""
MPH = 1609.344 / 3600


class _Grid:
    def to_latlon(self, text: str) -> tuple[float, float] | None:
        if "".join(text.split()).upper() == "11SMS0000000000":
            return (34.0, -118.0)
        return None


def _card():
    return parse_coord_card_text(CARD)


def _parse(text: str, *, card=None, grid=None, policy=None):
    return parse_tacrep_body(
        text,
        source_id="room-a",
        observed_at=T0,
        card=_card() if card is None else card,
        grid=_Grid() if grid is None else grid,
        policy=policy,
    )


def test_single_line_and_separator_spacing():
    lines = (
        "HAWK//I-103//11SMS0000000000//34.0 -118.0//90//25//stopping//tanks",
        "HAWK // I-103 // 11SMS0000000000 // 34.0 -118.0 // 90 // 25 // stopping // tanks",
        "HAWK' // I-103 // 11SMS0000000000 // 34.0 -118.0 // 90 // 25 // stopping // tanks",
        "HAWK //I-103// 11SMS0000000000 //34.0 -118.0// 90 //25 //stopping // tanks",
    )
    for line in lines:
        parsed = _parse(line)
        assert len(parsed) == 1
        message = parsed[0]
        assert message.source_message_id == "I-103"
        assert message.subject == ""
        assert message.callsign == "HAWK"
        assert message.tacrep_type == "IMINT"
        assert message.block_label == "I-100..I-199"
        assert message.block_warning == ""
        assert message.latitude == 34.0
        assert message.longitude == -118.0
        assert message.heading == 90
        assert message.event == "stopping"
        assert message.speed_mps is not None
        assert abs(message.speed_mps - 25 * MPH) < 1e-9
        assert ("remarks", "tanks") in message.attributes


def test_multiline_header_and_two_tacreps():
    text = """
    HAWK // I-103 // 11SMS0000000000 // 34.0 -118.0
    // 90 // 25 // stopping
    EAGLE // J-401 // 11SMS0000000000 // 34.2 -118.1 // 10 // 5 // milling
    """
    parsed = _parse(text)
    assert [item.source_message_id for item in parsed] == ["I-103", "J-401"]
    assert parsed[0].heading == 90
    assert parsed[0].event == "stopping"
    assert parsed[1].tacrep_type == "MTI"
    assert parsed[1].block_label == "J-400..J-499"
    assert parsed[1].event == "milling"


def test_mgrs_only_and_latlon_mismatch():
    mgrs_only = _parse("HAWK // I-110 // 11SMS0000000000 // // 0 // 0 // milling")
    assert mgrs_only[0].latitude == 34.0
    assert mgrs_only[0].longitude == -118.0
    assert mgrs_only[0].block_warning == ""
    mismatched = _parse("HAWK // I-111 // 11SMS0000000000 // 10.0 20.0 // 0 // 0 // stopping")
    assert mismatched[0].latitude == 10.0
    assert mismatched[0].longitude == 20.0
    assert "differ by" in mismatched[0].block_warning


def test_unknown_type_and_out_of_block_are_kept():
    unknown = _parse("HAWK // Q-100 // // 10 20 // 0 // 0 // stopping")
    assert unknown[0].source_message_id == "Q-100"
    assert unknown[0].tacrep_type == ""
    assert "unknown TACREP type" in unknown[0].block_warning
    outside = _parse("HAWK // I-250 // // 10 20 // 1 // 1 // stopping")
    assert outside[0].tacrep_type == "IMINT"
    assert outside[0].block_label == "I-100..I-199"
    assert "outside configured block" in outside[0].block_warning
    corrected = _parse("HAWK // I-103A // // 10 20 // 1 // 1 // stopping")
    assert corrected[0].serial_base == "I-103"
    assert corrected[0].source_message_id == "I-103A"
    assert corrected[0].block_label == "I-100..I-199"
    assert corrected[0].block_warning == ""


def test_correction_supersedes_and_distinct_serials_stay():
    first = _parse("HAWK // I-103 // // 10 20 // 1 // 1 // stopping")[0]
    newer = _parse("HAWK // I-103A // // 10.5 20.5 // 2 // 2 // milling")[0]
    other = _parse("HAWK // I-104 // // 11 21 // 3 // 3 // stopping")[0]
    kept = dedupe_messages((first, newer, other))
    assert [item.serial_full for item in kept] == ["I-103A", "I-104"]
    assert kept[0].history[0].serial_full == "I-103"
    late = dedupe_messages((newer, first))
    assert late[0].serial_full == "I-103A"
    assert late[0].latitude == 10.5
    assert late[0].history[-1].serial_full == "I-103"


def test_event_maps_onto_detector_evidence():
    from dataclasses import replace

    message = _parse("HAWK // I-103 // // 10 20 // 1 // 1 // stopping")[0]
    placed = replace(message, cell_id="cell")
    group = correlate((placed,))[0]
    kinds = [item.kind for item in correlation_evidence(group)]
    assert "stop" in kinds


def test_kind_seq_line_stays_a_fallback():
    policy = CorrelationPolicy(blocks=(TacrepBlock("I", 100, 199, "IMINT"),))
    parsed = _parse(
        "I-150//2026-01-01T00:00:00Z//10,20//subject=alpha",
        policy=policy,
    )
    assert parsed[0].kind == "I"
    assert parsed[0].subject == "alpha"
    assert parsed[0].tacrep_type == "IMINT"
    assert parsed[0].block_label == "I-100..I-199"
    legacy = _parse("O-7//2026-01-01T00:00:00Z//10.5,-80.25//subject=alpha")
    assert legacy[0].sequence == "7"
    assert legacy[0].subject == "alpha"


def test_invalid_coord_card_keeps_the_previous_file(tmp_path: Path):
    service = AnalyticsService(_settings(reference_dir=str(tmp_path)), None)
    saved = service.save_coord_card("coord-card.yaml", CARD.encode())
    assert saved["types"] == 2
    previous = (tmp_path / "coord-card.yaml").read_text(encoding="utf-8")
    with pytest.raises(CoordCardError):
        service.save_coord_card("coord-card.yaml", b"types: []\n")
    assert service._coord_card.types[0].name == "IMINT"
    assert (tmp_path / "coord-card.yaml").read_text(encoding="utf-8") == previous
    policy = AuthPolicy(read_token="read", write_token="write")
    denied = route_request(
        "POST",
        "/v1/coord-card",
        b"nope",
        {"authorization": "Bearer read"},
        service,
        auth=policy,
    )
    assert denied[0] == 401
    accepted = route_request(
        "POST",
        "/v1/coord-card",
        CARD.encode(),
        {"authorization": "Bearer write", "x-filename": "coord-card.yaml"},
        service,
        auth=policy,
    )
    assert accepted[0] == 200
    assert accepted[1]["types"] == 2


def test_example_cards_parse_and_are_not_the_development_default():
    yaml_card = parse_coord_card_text(
        Path("config/analytics/coord_card.example.yaml").read_text(encoding="utf-8"),
        suffix=".yaml",
    )
    csv_card = parse_coord_card_text(
        Path("config/analytics/coord_card.example.csv").read_text(encoding="utf-8"),
        suffix=".csv",
    )
    json_card = parse_coord_card_text(
        Path("config/analytics/coord_card.example.json").read_text(encoding="utf-8"),
        suffix=".json",
    )
    assert [item.name for item in yaml_card.types] == ["MTI", "IMINT", "ELINT", "FUSED"]
    assert yaml_card.types[2].kinds == ("E", "G")
    assert [item.kind for item in csv_card.types] == ["J", "I", "E", "F"]
    assert csv_card.types[2].kinds == ("E", "G")
    assert [item.name for item in json_card.types] == ["MTI", "IMINT", "ELINT", "FUSED"]
    assert json_card.schema_version == "1"
    assert json_card.types[2].kinds == ("E", "G")
    assert _settings().coord_card_path == ""
    point = MgrsLibrary().to_latlon("11SMS0000000000")
    assert point is not None
    assert abs(point[0] - 32.53) < 0.1


def test_sessions_use_the_gap_from_the_first_message():
    from analytics.domain.correlation import TacrepMessage

    def message(kind: str, minute: float, cell: str, bmoa: str = "AREA") -> TacrepMessage:
        return TacrepMessage(
            kind=kind,
            sequence="1",
            observed_at=T0 + timedelta(minutes=minute),
            latitude=10,
            longitude=20,
            source_id="a",
            source_message_id=f"{kind}-{minute}",
            bmoa=bmoa,
            cell_id=cell,
        )

    assert len(correlate((message("O", 10, "cell"), message("J", 35, "cell")))) == 1
    assert len(correlate((message("O", 0, "cell"), message("J", 25, "cell")))) == 1
    assert (
        correlate((message("O", 10, "cell"), message("J", 35, "cell")))[0].activity.state
        == "transload"
    )
    assert len(correlate((message("O", 0, "cell"), message("J", 45, "cell")))) == 1
    assert len(correlate((message("O", 0, "cell"), message("J", 45.1, "cell")))) == 2

    def neighbors(cell: str, k: int) -> set[str]:
        del k
        if cell == "cell":
            return {"cell", "near"}
        return {cell}

    joined = correlate(
        (message("O", 0, "cell"), message("J", 5, "near")),
        neighbors=neighbors,
    )
    apart = correlate(
        (message("O", 0, "cell"), message("J", 5, "far")),
        neighbors=neighbors,
    )
    assert len(joined) == 1
    assert len(apart) == 2


def test_naive_timestamps_and_invalid_positions_do_not_crash_analysis():
    service = AnalyticsService(_settings(), None)
    service.add_fixes(
        [
            GpsFix("v", 10, 20, datetime(2026, 1, 1, 12, 0, 0), 0),
            GpsFix("v", 10, 20, datetime(2026, 1, 1, 12, 5, tzinfo=UTC), 0),
            GpsFix("bad", 91, 0, datetime(2026, 1, 1, tzinfo=UTC), 0),
        ]
    )
    service.analyze()
    assert len(service._fixes) == 2
    assert service._fixes[0].timestamp.tzinfo is UTC


def test_analysis_loop_logs_and_continues(caplog: pytest.LogCaptureFixture):
    service = AnalyticsService(_settings(analysis_interval_seconds=0), None)
    calls = {"n": 0}

    def boom() -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom")
        raise asyncio.CancelledError

    service.analyze = boom  # type: ignore[method-assign]
    with caplog.at_level("ERROR"), pytest.raises(asyncio.CancelledError):
        asyncio.run(service.run_analysis_loop())
    assert calls["n"] == 2
    assert "Analysis failed" in caplog.text


def test_health_stays_open_and_writes_need_the_write_token():
    service = AnalyticsService(_settings(), None)
    policy = AuthPolicy(read_token="read", write_token="write")
    assert route_request("GET", "/health", b"", {}, service, auth=policy)[0] == 200
    assert route_request("GET", "/api/sites", b"", {}, service, auth=policy)[0] == 401
    listed = route_request(
        "GET",
        "/api/sites",
        b"",
        {"authorization": "Bearer write"},
        service,
        auth=policy,
    )
    assert listed[0] == 200
