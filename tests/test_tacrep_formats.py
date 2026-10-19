"""Parser tests for the shipped MTI, IMINT, ELINT, and FUSED layouts."""

from datetime import UTC, datetime
from pathlib import Path

from tests.test_analytics_reports import _settings

from analytics.composition import wire_service as AnalyticsService
from analytics.domain.assessment import ConfidencePolicy, _numeric_confidence
from analytics.domain.coordcard import CoordCardError, parse_coord_card_text
from analytics.domain.correlation import (
    CorrelationPolicy,
    correlate,
    correlation_evidence,
    dedupe_messages,
    pair_score,
)
from analytics.domain.positions import NAUTICAL_MILE_METERS, point_within_ellipse
from analytics.domain.reference import Bmoa, Evidence, Msr, named_bmoa, named_msr
from analytics.domain.tacrep_parse import parse_tacrep_body

T0 = datetime(2026, 1, 1, 20, 0, tzinfo=UTC)
CARD = parse_coord_card_text(
    Path("config/analytics/coord_card.example.yaml").read_text(encoding="utf-8"),
    suffix=".yaml",
)


class _Grid:
    def to_latlon(self, text: str) -> tuple[float, float] | None:
        if text == "11SMS0000000000":
            return (32.5, -118.0)
        return None


def _parse(text: str, card: object = CARD, when: datetime = T0) -> tuple:
    return parse_tacrep_body(
        text,
        source_id="room-1",
        observed_at=when,
        card=card,  # type: ignore[arg-type]
        grid=_Grid(),
    )


def test_mti_layout_reads_activity_location_fillins_and_links():
    message = _parse(
        "HAWK // J-401 // MILLING // BULL-1 // 12345 // "
        "N6919 W11919 // ALPHA / MSR RED // CAT II // 1919Z // "
        "3 VEHICLES 25 MPH HDG 270"
    )[0]
    assert message.tacrep_type == "MTI"
    assert message.serial_base == "J-401"
    assert message.subject == ""
    assert message.bullseye == "BULL-1"
    assert ("link16", "12345") in message.attributes
    assert abs(message.latitude - (69 + 19 / 60)) < 1e-9
    assert abs(message.longitude - -(119 + 19 / 60)) < 1e-9
    assert message.bmoa == "ALPHA"
    assert message.msr == "RED"
    assert message.cat_level == 2
    assert message.ce_radius_m == 15
    assert message.observed_at == datetime(2026, 1, 1, 19, 19, tzinfo=UTC)
    assert message.vehicle_count == 3
    assert message.speed_mps is not None
    assert abs(message.speed_mps - 25 * 1609.344 / 3600) < 1e-9
    assert message.heading == 270
    assert message.events == ("milling",)
    assert named_bmoa("TAI ALPHA", (Bmoa(name="ALPHA", polygon=()),)) == "ALPHA"
    assert named_msr("MSR RED", (Msr(name="RED", points=((1.0, 2.0), (1.1, 2.1))),)) == "RED"


def test_decimal_minutes_mgrs_and_mover_rotator_halt():
    decimal = _parse(
        "HAWK // J-402 // MOVER // B // 1 // N3712.34 E12812.34 // TAI // CAT I // 1919Z // "
    )[0]
    assert abs(decimal.latitude - (37 + 12.34 / 60)) < 1e-9
    assert abs(decimal.longitude - (128 + 12.34 / 60)) < 1e-9
    assert decimal.events == ("mover",)
    mgrs = _parse(
        "HAWK // J-403 // ROTATOR // B // 1 // 11SMS0000000000 // MSR RED // CAT I // 1919Z // HALT"
    )[0]
    assert (mgrs.latitude, mgrs.longitude) == (32.5, -118.0)
    assert mgrs.mgrs_text == "11SMS0000000000"
    assert mgrs.events == ("rotator", "halt")
    assert mgrs.msr == "RED"


def test_imint_layout():
    message = _parse(
        "HAWK // I-150 // TEL // N3712.34 E12812.34 // 1200 // TAI-7 // 1919Z // CAT III"
    )[0]
    assert message.tacrep_type == "IMINT"
    assert ("equipment", "TEL") in message.attributes
    assert ("elevation", "1200") in message.attributes
    assert message.bmoa == "TAI-7"
    assert message.cat_level == 3
    assert message.ce_radius_m == 30
    assert message.observed_at == datetime(2026, 1, 1, 19, 19, tzinfo=UTC)


def test_elint_e_and_g_share_a_type_and_tolerate_a_tai_on_the_location():
    card = parse_coord_card_text(
        Path("config/analytics/coord_card.example.yaml").read_text(encoding="utf-8").replace(
            "magnetic_declination_degrees: 0",
            "magnetic_declination_degrees: 2",
        ),
        suffix=".yaml",
    )
    message = _parse(
        "HAWK // G-501 // COMEXT // ACTIVE // N6919 W11919, NORTH // SOUTH // "
        "10/4 // 045 // CAT IV // TOI 1919Z // 2 VEHICLES",
        card,
    )[0]
    other = _parse(
        "HAWK // E-510 // RADAR // AWAKE // N6919 W11919 // TAI-1 // "
        "1/1 // 000 // CAT I // 1919Z //",
        card,
    )[0]
    assert message.tacrep_type == "ELINT"
    assert other.tacrep_type == "ELINT"
    assert message.bmoa == "SOUTH"
    assert "location TAI NORTH differs from TAI field SOUTH" in message.block_warning
    assert message.semi_major_m == 10 * NAUTICAL_MILE_METERS
    assert message.semi_minor_m == 4 * NAUTICAL_MILE_METERS
    assert message.orientation_deg == 47
    assert message.cat_level == 4
    assert message.ce_radius_m == 91
    assert ("entity", "COMEXT") in message.attributes
    assert message.events == ()
    assert message.vehicle_count == 2
    assert card.bullseye_name == ""
    assert card.ce_radius_m(1) == 6
    tight = parse_coord_card_text(
        "types:\n- name: ELINT\n  kinds: [E, G]\n  start: 500\n  end: 599\n"
        "  fields: [callsign, id]\ncat_ce:\n  unit: m\n  radii: {I: 5, IV: 40}\n",
        suffix=".yaml",
    )
    assert tight.ce_radius_m(1) == 5
    assert tight.ce_radius_m(4) == 40


def test_meters_on_the_ellipse_field_override_the_card_unit():
    message = _parse(
        "HAWK // E-520 // RADAR // ACTIVE // 10.0, 20.0 // TAI // "
        "2000/1000 m // 0 // CAT II // 1919Z //"
    )[0]
    assert message.semi_major_m == 2000
    assert message.semi_minor_m == 1000


def test_clock_rolls_over_midnight_and_reads_a_day_group():
    after_midnight = datetime(2026, 1, 2, 0, 10, tzinfo=UTC)
    rolled = _parse(
        "HAWK // J-404 // MOVER // B // 1 // 10.0 20.0 // TAI // CAT I // 1919Z //",
        when=after_midnight,
    )[0]
    assert rolled.observed_at == datetime(2026, 1, 1, 19, 19, tzinfo=UTC)
    dated = _parse(
        "HAWK // J-405 // MOVER // B // 1 // 10.0 20.0 // TAI // CAT I // 011919Z //",
        when=after_midnight,
    )[0]
    assert dated.observed_at == datetime(2026, 1, 1, 19, 19, tzinfo=UTC)


def test_update_supersedes_and_fused_messages_keep_correlation_links():
    original = _parse(
        "HAWK // G-501 // COMEXT // ACTIVE // N6919 W11919 // TAI // "
        "10/4 // 0 // CAT II // 1919Z //"
    )[0]
    update = _parse(
        "HAWK // UPDATE G-501 // POSS HIGH TEL // N6919 W11919 // CAT IV // "
        "TOI 1919Z // CORRELATES TO J-401"
    )[0]
    assert update.serial_base == "G-501"
    assert update.serial_full == "UPDATE G-501"
    assert update.correction_rank > original.correction_rank
    assert update.subject == "TEL"
    assert update.confidence_phrase == "POSS HIGH"
    assert update.confidence_level == "possible"
    assert update.confidence_band == "high"
    assert update.correlates_to == ("J-401",)
    assert update.tacrep_type == "ELINT"
    winner = dedupe_messages((original, update))
    assert len(winner) == 1
    assert winner[0].serial_full == "UPDATE G-501"
    assert winner[0].history[0].serial_full == "G-501"
    late = dedupe_messages((update, original))
    assert late[0].serial_full == "UPDATE G-501"
    assert late[0].history[0].serial_full == "G-501"
    fused = _parse(
        "HAWK // F-901 // FUSED CALL // N6919 W11919 // CAT III // 1919Z // "
        "CORRELATES TO J-401, G-501"
    )[0]
    assert fused.tacrep_type == "FUSED"
    assert fused.correlates_to == ("J-401", "G-501")
    assert fused.subject == "FUSED CALL"


def test_ellipse_match_suggests_a_fused_correlation_without_sending_one():
    center = 69 + 19 / 60
    west = -(119 + 19 / 60)
    elint = _parse(
        "HAWK // G-501 // COMEXT // ACTIVE // N6919 W11919 // TAI // "
        "10/10 // 0 // CAT IV // 1919Z //"
    )[0]
    halt = _parse(
        "HAWK // J-401 // MOVER // B // 1 // "
        f"{center + 0.01}, {west} // TAI // CAT II // 1919Z // HALT"
    )[0]
    assert point_within_ellipse(
        halt.latitude,
        halt.longitude,
        elint.latitude,
        elint.longitude,
        elint.semi_major_m or 0,
        elint.semi_minor_m or 0,
        elint.orientation_deg or 0,
    )
    outside = _parse(
        "HAWK // J-406 // MOVER // B // 1 // 10.0, 20.0 // TAI // CAT I // 1919Z // HALT"
    )[0]
    assert not point_within_ellipse(
        outside.latitude,
        outside.longitude,
        elint.latitude,
        elint.longitude,
        elint.semi_major_m or 0,
        elint.semi_minor_m or 0,
        elint.orientation_deg or 0,
    )
    from dataclasses import replace

    grouped = correlate(
        (
            replace(elint, cell_id="elint-cell"),
            replace(halt, cell_id="mti-cell"),
        )
    )
    assert len(grouped) == 1
    fusion = grouped[0].suggested_fusion
    assert fusion is not None
    assert fusion.anchor_id == "J-401"
    assert fusion.linked_ids == ("G-501", "J-401")
    assert "ellipse" in fusion.reason
    assert fusion.tacrep_type == "FUSED"
    separate = correlate(
        (
            replace(elint, cell_id="elint-cell"),
            replace(outside, cell_id="other-cell"),
        )
    )
    assert len(separate) == 2
    assert all(item.suggested_fusion is None for item in separate)
    kinds = {item.kind for item in correlation_evidence(grouped[0])}
    assert "stop" in kinds
    assert "movement" in kinds


def test_explicit_links_boost_score_and_confidence_and_show_on_evidence():
    left = _parse(
        "HAWK // F-901 // SAME // N6919 W11919 // CAT IV // 1919Z // CORRELATES TO J-401"
    )[0]
    right = _parse(
        "HAWK // J-401 // MOVER // B // 1 // N6919 W11919 // TAI // CAT II // 1919Z //"
    )[0]
    from dataclasses import replace

    pair = (
        replace(left, cell_id="fused", subject=""),
        replace(right, cell_id="mti", subject=""),
    )
    boosted = pair_score(pair[0], pair[1], CorrelationPolicy())
    plain = pair_score(replace(pair[0], correlates_to=()), pair[1], CorrelationPolicy())
    assert boosted > plain
    assert boosted <= 1
    grouped = correlate(pair)
    evidence = correlation_evidence(grouped[0])
    link = next(item for item in evidence if item.kind == "explicit_link")
    assert link.weight == 0.5
    assert "J-401" in link.linked_ids
    assert "F-901" in link.linked_ids
    assert _numeric_confidence(
        (Evidence(kind="explicit_link", weight=0.5, detail="links"),),
        ConfidencePolicy(),
    ) == 0.5


def test_custom_fields_remain_attributes_and_overlapping_kinds_fail():
    card = parse_coord_card_text(
        "types:\n- name: NOTE\n  kind: Q\n  start: 100\n  end: 199\n"
        "  fields: [callsign, id, location, remarks]\n",
        suffix=".yaml",
    )
    message = _parse("HAWK // Q-100 // 10.0, 20.0 // hello", card)[0]
    assert ("remarks", "hello") in message.attributes
    try:
        parse_coord_card_text(
            "types:\n"
            "- {name: A, kind: E, start: 100, end: 150, fields: [id]}\n"
            "- {name: B, kinds: [E, G], start: 140, end: 160, fields: [id]}\n",
            suffix=".yaml",
        )
    except CoordCardError as exc:
        assert "overlap" in str(exc)
    else:
        raise AssertionError("expected an overlap error")


def test_service_snapshot_carries_the_suggestion_for_the_websocket():
    service = AnalyticsService(
        _settings(coord_card_path="config/analytics/coord_card.example.yaml"),
        None,
    )
    service._bmoas = (Bmoa(name="ALPHA", polygon=()),)
    service._msrs = (Msr(name="RED", points=((69.3, -119.3), (69.4, -119.2))),)
    observed = T0.isoformat()
    service.ingest_tacrep_body(
        "HAWK // G-501 // COMEXT // ACTIVE // N6919 W11919 // ALPHA // 10/10 // 0 // "
        f"CAT IV // {observed} //",
        "room-1",
    )
    service.ingest_tacrep_body(
        "HAWK // J-401 // MILLING // BULL-1 // 9 // "
        f"{69 + 19 / 60 + 0.01}, {-(119 + 19 / 60)} // ALPHA / MSR RED // CAT II // "
        f"{observed} // HALT",
        "room-1",
    )
    assert service._messages[0].bmoa == "ALPHA"
    snapshot = service.output_snapshot()
    suggested = snapshot["suggested_fused"]
    assert isinstance(suggested, list) and suggested
    assert suggested[0]["anchor_id"] == "J-401"
    assert suggested[0]["sent"] is False
    document = service.correlations_document()
    rows = document["correlations"]
    assert isinstance(rows, list)
    assert rows[0]["suggested_fused"]["sent"] is False
