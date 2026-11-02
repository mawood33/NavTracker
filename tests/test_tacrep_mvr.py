"""Companion-tool MTI lines: MVR, cardinals, IVO, and LINK-16 tracks."""

import logging
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from tests.test_analytics_reports import _settings

from analytics.composition import wire_service as AnalyticsService
from analytics.domain.coordcard import CoordCard
from analytics.domain.correlation import correlate
from analytics.domain.positions import is_movement_field, movement_heading, parse_fillins
from analytics.domain.tacrep_parse import parse_tacrep_body
from analytics.domain.tracks import canonical_link16, link16_vehicle_id

T0 = datetime(2026, 1, 1, 20, 0, tzinfo=UTC)
CARD = Path("config/analytics/coord_card.example.yaml").read_text(encoding="utf-8")
MOVING = (
    "REAPER 21//TACREP 1//1 MVR SW//BULL-1//01000//N3712.34 E12812.34//"
    "MSR BURGUNDY//-//1910Z//1 MVR, 9 MPH, SW IVO 10105"
)
HALT = (
    "REAPER 21//TACREP 2//1 MVR HALT//BULL-1//01000//N3713.10 E12812.00//"
    "MSR BURGUNDY//-//1915Z//1 MVR HALT IVO 10105"
)


def _parse(text: str, card: CoordCard | None = None) -> tuple:
    from analytics.domain.coordcard import parse_coord_card_text

    active = card if card is not None else parse_coord_card_text(CARD, suffix=".yaml")
    return parse_tacrep_body(text, source_id="room-1", observed_at=T0, card=active)


def test_moving_report_reads_count_speed_heading_route_and_ivo():
    message = _parse(MOVING)[0]
    assert message.tacrep_type == "MTI"
    assert message.serial_full == "TACREP 1"
    assert message.serial_base == "TACREP-1"
    assert message.kind == "J"
    assert message.events == ("mover",)
    assert message.vehicle_count == 1
    assert message.heading == 225
    assert message.speed_mps is not None
    assert abs(message.speed_mps - 9 * 1609.344 / 3600) < 1e-9
    assert message.msr == "BURGUNDY"
    assert message.bullseye == "BULL-1"
    assert ("link16", "01000") in message.attributes
    assert ("ivo", "10105") in message.attributes
    assert message.block_warning == ""
    assert abs(message.latitude - (37 + 12.34 / 60)) < 1e-9
    assert abs(message.longitude - (128 + 12.34 / 60)) < 1e-9


def test_halt_has_no_speed_or_heading_and_still_counts_the_mover():
    message = _parse(HALT)[0]
    assert message.events == ("halt",)
    assert message.vehicle_count == 1
    assert message.speed_mps is None
    assert message.heading is None
    assert ("ivo", "10105") in message.attributes
    count, speed, heading, halted = parse_fillins("1 MVR HALT IVO NORTH PAD")
    assert (count, speed, heading, halted) == (1, None, None, True)
    assert parse_fillins("1 MVR, 9 MPH, NNE")[2] is None
    assert parse_fillins("3 VEH W/ TRAILER")[2] is None
    assert parse_fillins("3 VEHICLES 25 MPH HDG 270 SW")[2] == 270


def test_compass_heading_comes_only_from_the_movement_field():
    assert movement_heading("1 MVR N") == 0
    assert movement_heading("1 MVR NE") == 45
    assert movement_heading("1 MVR E") == 90
    assert movement_heading("1 MVR SE") == 135
    assert movement_heading("1 MVR S") == 180
    assert movement_heading("1 MVR SW") == 225
    assert movement_heading("1 MVR W") == 270
    assert movement_heading("1 MVR NW") == 315
    assert movement_heading("MVR NNE") == 22.5
    assert movement_heading("1 MVR") is None
    assert movement_heading("MVR") is None
    assert movement_heading("1 MVR HALT") is None
    assert movement_heading("STATIONARY") is None
    assert movement_heading("HALT") is None
    assert movement_heading("STOP") is None
    assert movement_heading("MILL") is None
    assert movement_heading("ROTATOR") is None
    assert movement_heading("SLOW") is None
    for text in (
        "1 MVR",
        "1 MVR N",
        "1 MVR HALT",
        "MVR",
        "STATIONARY",
        "HALT",
        "STOP",
        "STOPPED",
        "MILL",
        "MILLING",
        "ROTATOR",
        "SLOW",
        "SLOWING",
        "SLOWDOWN",
        "MOVEMENT",
        "MOVER",
    ):
        assert is_movement_field(text) is True
    assert is_movement_field("3 VEH W/ TRAILER") is False
    assert movement_heading("3 VEH W/ TRAILER") is None
    assert movement_heading("seen W of the pad") is None
    remarks = MOVING.replace("//1 MVR SW//", "//1 MVR//").replace(
        "1 MVR, 9 MPH, SW IVO 10105",
        "3 VEH W/ TRAILER, seen N of the pad",
    )
    plain = _parse(remarks)[0]
    assert plain.heading is None
    assert _parse(MOVING)[0].heading == 225


def test_j_report_with_link16_is_not_a_track_and_does_not_join_by_id():
    text = (
        "REAPER 21//J-401//1 MVR//BULL-1//01000//N3712.34 E12812.34//"
        "MSR BURGUNDY//-//1910Z//1 MVR"
    )
    message = _parse(text)[0]
    assert message.kind == "J"
    assert message.serial_base == "J-401"
    assert ("link16", "01000") in message.attributes
    service = AnalyticsService(
        _settings(
            environment="development",
            coord_card_path="config/analytics/coord_card.example.yaml",
        )
    )
    service.ingest_tacrep_body(text, "dev")
    assert service.tracks() == []
    far = replace(message, cell_id="far")
    near = replace(message, cell_id="near")
    assert len(correlate((near, far))) == 2


def test_tacrep_id_parses_on_an_empty_card_and_does_not_collapse_the_series():
    messages = parse_tacrep_body(
        MOVING + "\n" + HALT,
        source_id="room-1",
        observed_at=T0,
        card=CoordCard(),
    )
    assert [item.serial_full for item in messages] == ["TACREP 1", "TACREP 2"]
    assert {item.tacrep_type for item in messages} == {"MTI"}
    assert canonical_link16("01000") == "01000"
    assert canonical_link16("a1b2c") == "A1B2C"
    assert canonical_link16("1000") == ""
    assert canonical_link16("010000") == ""
    assert link16_vehicle_id((("link16", "a1b2c"),)) == "link16:A1B2C"


def test_invalid_link16_is_ignored(caplog):
    caplog.set_level(logging.WARNING)
    text = MOVING.replace("//01000//", "//1000//")
    message = _parse(text)[0]
    assert ("link16", "1000") in message.attributes
    assert link16_vehicle_id(message.attributes) == ""
    assert "Ignoring LINK-16" in caplog.text
    letters = _parse(MOVING.replace("//01000//", "//A1B2C//"))[0]
    assert link16_vehicle_id(letters.attributes) == "link16:A1B2C"
    service = AnalyticsService(
        _settings(
            environment="development",
            coord_card_path="config/analytics/coord_card.example.yaml",
        )
    )
    service.ingest_tacrep_body(MOVING.replace("//01000//", "//a1b2c//"), "dev")
    assert [fix.vehicle_id for fix in service.tracks()] == ["link16:A1B2C"]


def test_same_link16_joins_distant_reports_inside_the_window():
    first, second = _parse(MOVING + "\n" + HALT)
    grouped = correlate(
        (
            replace(first, cell_id="near"),
            replace(second, cell_id="far"),
        )
    )
    assert len(grouped) == 1
    assert len(grouped[0].messages) == 2
    other = replace(second, attributes=(("link16", "01001"),), cell_id="far")
    assert len(correlate((replace(first, cell_id="near"), other))) == 2


def test_ingested_series_is_one_track_per_link16_and_can_mark_transload():
    service = AnalyticsService(
        _settings(
            environment="development",
            reference_dir="fixtures/dev/reference",
            coord_card_path="config/analytics/coord_card.example.yaml",
        )
    )
    service.load_reference()
    body = Path("fixtures/dev/tacrep_track.txt").read_text(encoding="utf-8")
    accepted = service.ingest_tacrep_body(body, "dev-replay")
    assert accepted["accepted"] == 5
    service.analyze()
    vehicles = {fix.vehicle_id for fix in service.tracks()}
    assert vehicles == {"link16:01000", "link16:01001"}
    moving = [
        fix
        for fix in service.tracks()
        if fix.vehicle_id == "link16:01000" and fix.speed_mps
    ]
    assert len(moving) == 1
    assert moving[0].heading == 225
    halted = [
        fix
        for fix in service.tracks()
        if fix.vehicle_id == "link16:01000" and fix.speed_mps == 0
    ]
    assert len(halted) == 2
    matched = [
        area
        for area in service.areas()
        if area.matched_site == "10105" and "transload" in area.supported_types
    ]
    assert matched
    assert set(matched[0].vehicle_ids) >= {"link16:01000", "link16:01001"}
    assert matched[0].matched_msr == "MSR BURGUNDY"
