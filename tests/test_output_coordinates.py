"""Published ChatSurfer coordinates: MGRS, decimal lat/lon, and the analyst override."""

import asyncio
import json
import logging
from pathlib import Path

import pytest
from tests.test_analytics_reports import _settings
from tests.test_message_rooms import ADHOC, SITREP

from analytics.composition import wire_service as AnalyticsService
from analytics.domain.access import AuthPolicy
from analytics.domain.card_schema import bundled_catalog, validate_card_document
from analytics.domain.coordinates import (
    _FORMATS,
    FormattedPoint,
    OutputCoordinates,
    format_point,
    known_coordinate_formats,
    output_from_mapping,
    parse_formatted,
    register_coordinate_format,
    resolve_output_coordinates,
    rewrite_published_location,
    within_precision,
)
from analytics.domain.level_assessment import annotate_report
from analytics.domain.positions import parse_location
from analytics.infrastructure.config_output import output_coord_settings
from analytics.infrastructure.http_api import route_request
from analytics.infrastructure.mgrs_grid import MgrsLibrary

CARD = Path("config/analytics/coord_card.example.yaml")
ELINT = (
    "HAWK // G-501 // POSS LOW TEL // ACTIVE // N6919 W11919 // TAI // - // - // "
    "CAT IV // 1919Z //"
)
ENV_NAMES = (
    "ANALYTICS_OUTPUT_COORD_FORMAT",
    "ANALYTICS_OUTPUT_MGRS_PRECISION_M",
    "ANALYTICS_OUTPUT_MGRS_SPACES",
    "ANALYTICS_OUTPUT_LATLON_DECIMALS",
    "ANALYTICS_OUTPUT_LATLON_SEPARATOR",
    "ANALYTICS_OUTPUT_LATLON_HEMISPHERE",
)


class _Publisher:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    async def publish_to(self, room: str, text: str) -> None:
        self.sent.append((room, text))


class _SilentGrid:
    def to_latlon(self, text: str) -> None:
        del text
        return None

    def to_mgrs(self, latitude: float, longitude: float, precision_m: int) -> None:
        del latitude, longitude, precision_m
        return None


def _clear_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ENV_NAMES:
        monkeypatch.delenv(name, raising=False)


def _round_trip(point: tuple[float, float], spec: OutputCoordinates, grid: MgrsLibrary) -> None:
    formatted = format_point(point[0], point[1], spec, grid)
    assert formatted.text
    if spec.format == "mgrs" and formatted.warning:
        assert "decimal lat/lon" in formatted.warning
    elif spec.format == "ddmm":
        assert "not recognized" in formatted.warning
    else:
        assert formatted.warning == ""
    parsed = parse_formatted(formatted.text, grid)
    assert parsed is not None
    if formatted.warning:
        check = OutputCoordinates(format="latlon", decimals=spec.decimals)
    else:
        check = spec
    assert within_precision(point, parsed, check)


def test_default_mgrs_round_trips_zones_hemispheres_and_poles() -> None:
    grid = MgrsLibrary()
    origin = parse_location("N6919 W11919", grid)
    assert origin is not None
    compact = format_point(origin[0], origin[1], OutputCoordinates(), grid)
    assert compact.text == "11WMS0869991399"
    assert compact.warning == ""
    assert not compact.text.startswith("N")
    spaced = format_point(origin[0], origin[1], OutputCoordinates(spaces=True), grid)
    assert spaced.text == "11W MS 08699 91399"
    suffix = format_point(origin[0], origin[1], OutputCoordinates(format="latlon"), grid)
    assert suffix.text == "69.31667N 119.31667W"
    signed = format_point(
        origin[0],
        origin[1],
        OutputCoordinates(format="latlon", hemisphere="signed"),
        grid,
    )
    assert signed.text == "69.31667 -119.31667"
    comma = format_point(
        origin[0],
        origin[1],
        OutputCoordinates(format="latlon", separator=","),
        grid,
    )
    assert comma.text == "69.31667N,119.31667W"
    norway = format_point(60.0, 5.0, OutputCoordinates(), grid)
    assert norway.text.startswith("32V")
    svalbard = format_point(78.2232, 15.6267, OutputCoordinates(), grid)
    assert svalbard.text.startswith("33X")
    south = format_point(-33.8688, 151.2093, OutputCoordinates(), grid)
    assert south.text.startswith("56H")
    points = (
        origin,
        (60.0, 5.0),
        (78.2232, 15.6267),
        (78.0, 25.0),
        (-33.8688, 151.2093),
        (89.9, 10.0),
        (-88.0, 40.0),
    )
    specs = (
        OutputCoordinates(),
        OutputCoordinates(precision_m=10),
        OutputCoordinates(precision_m=100),
        OutputCoordinates(format="latlon"),
        OutputCoordinates(format="latlon", hemisphere="signed", decimals=4),
        OutputCoordinates(format="ddmm"),
    )
    for point in points:
        for spec in specs:
            _round_trip(point, spec, grid)
    polar = format_point(89.9, 10.0, OutputCoordinates(), grid)
    assert polar.text[0] in "ABYZ"
    ddmm = format_point(
        origin[0], origin[1], OutputCoordinates(format="ddmm", decimals=0), grid
    )
    assert ddmm.text == "N6919 W11919"
    assert "not recognized" in ddmm.warning
    assert parse_location("69.31667N 119.31667W", grid) is None
    assert parse_formatted("69.31667N 119.31667W", grid) == parse_formatted(suffix.text, grid)


def test_mgrs_failure_falls_back_to_decimal_latlon() -> None:
    formatted = format_point(10.0, 20.0, OutputCoordinates(), _SilentGrid())
    assert formatted.text == "10.00000N 20.00000E"
    assert formatted.warning == "MGRS conversion failed; using decimal lat/lon"
    unknown = format_point(10.0, 20.0, OutputCoordinates(format="grid"), _SilentGrid())
    assert "unknown output coordinate format" in unknown.warning
    assert unknown.text.endswith("N") or "E" in unknown.text

    def _custom(
        latitude: float,
        longitude: float,
        spec: OutputCoordinates,
        grid: object,
    ) -> FormattedPoint:
        del latitude, longitude, spec, grid
        return FormattedPoint("POINT")

    register_coordinate_format("custom", _custom)
    try:
        assert "custom" in known_coordinate_formats()
        assert format_point(1.0, 2.0, OutputCoordinates(format="custom")).text == "POINT"
    finally:
        _FORMATS.pop("custom", None)


def test_rewrite_keeps_the_field_count_for_the_assessment_remark() -> None:
    raw = "HAWK // G-501 // POSS LOW TEL // N6919 W11919 // TAI // 1919Z // CAT IV"
    rewritten = rewrite_published_location(raw, "N6919 W11919", "11WMS0869991399")
    assert rewritten.count("//") == raw.count("//")
    assert "N6919" not in rewritten
    body = annotate_report(rewritten, "SINGLE SOURCE POSS LOW")
    assert body.endswith("CAT IV SINGLE SOURCE POSS LOW")
    assert body.count("//") == raw.count("//")
    assert rewrite_published_location(raw, "N6919 W11919", "11W // MS") == raw
    tail = "4. N6919 W11919, 120"
    assert rewrite_published_location(tail, "N6919 W11919", "11WMS0869991399") == (
        "4. 11WMS0869991399, 120"
    )


def test_precedence_is_override_then_card_then_env_then_mgrs() -> None:
    env = OutputCoordinates(format="latlon")
    card = output_from_mapping(
        {
            "coordinate_format": "mgrs",
            "types": {"SITREP": {"coordinate_format": "latlon"}, "TACREP": {"spaces": True}},
        }
    )
    override = OutputCoordinates(format="latlon", decimals=3)
    spec, source = resolve_output_coordinates(
        override, card, env, env_configured=True, message_type="ELINT"
    )
    assert (spec.format, source) == ("latlon", "override")
    spec, source = resolve_output_coordinates(
        None, card, env, env_configured=True, message_type="SITREP"
    )
    assert (spec.format, source) == ("latlon", "coord_card")
    spec, source = resolve_output_coordinates(
        None, card, env, env_configured=True, message_type="ELINT"
    )
    assert (spec.format, spec.spaces, source) == ("mgrs", True, "coord_card")
    spec, source = resolve_output_coordinates(None, None, env, env_configured=True)
    assert source == "env"
    spec, source = resolve_output_coordinates(None, None, env, env_configured=False)
    assert (spec.format, source) == ("latlon", "default")
    with pytest.raises(ValueError, match="Unknown coordinate format"):
        output_from_mapping({"coordinate_format": "n6919"})


def test_env_and_yaml_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_env(monkeypatch)
    plain = output_coord_settings({})
    assert plain["output_coord_format"] == "mgrs"
    assert plain["output_coord_configured"] is False
    chosen = output_coord_settings(
        {"output": {"coordinate_format": "latlon", "latlon_decimals": 4}}
    )
    assert chosen["output_coord_format"] == "latlon"
    assert chosen["output_latlon_decimals"] == 4
    assert chosen["output_coord_configured"] is True
    monkeypatch.setenv("ANALYTICS_OUTPUT_COORD_FORMAT", "mgrs")
    monkeypatch.setenv("ANALYTICS_OUTPUT_MGRS_PRECISION_M", "10")
    from_env = output_coord_settings({"output": {"coordinate_format": "latlon"}})
    assert from_env["output_coord_format"] == "mgrs"
    assert from_env["output_mgrs_precision_m"] == 10
    monkeypatch.setenv("ANALYTICS_OUTPUT_COORD_FORMAT", "n6919")
    with pytest.raises(RuntimeError, match="ANALYTICS_OUTPUT_COORD_FORMAT"):
        output_coord_settings({})


def test_example_cards_select_mgrs_and_latlon_by_type() -> None:
    from analytics.domain.coordcard import parse_coord_card_text

    for suffix in (".yaml", ".json", ".csv"):
        path = Path(f"config/analytics/coord_card.example{suffix}")
        card = parse_coord_card_text(path.read_text(encoding="utf-8"), suffix=suffix)
        assert card.output_coordinates is not None
        assert card.output_coordinates.format == "mgrs"
        typed = dict(card.output_coordinates.by_type)
        assert typed["SITREP"].format == "latlon"
        assert typed["ADHOC"].format == "latlon"
    raw = Path("config/analytics/coord_card.example.json").read_text(encoding="utf-8")
    document = json.loads(raw)
    assert validate_card_document(document, bundled_catalog()) == "1"


def test_publish_rewrites_location_and_keeps_received_text(tmp_path: Path) -> None:
    publisher = _Publisher()
    service = AnalyticsService(
        _settings(reference_dir=str(tmp_path)),
        None,
        publisher=publisher,
    )
    service.save_coord_card("coord-card.yaml", CARD.read_bytes())
    service.ingest_tacrep_body(ELINT, "room-elint-ingest")
    service.ingest_tacrep_body(SITREP, "room-sitrep-ingest")
    service.ingest_tacrep_body(ADHOC, "room-adhoc-ingest")
    stored = {message.tacrep_type: message for message in service._messages}
    assert stored["ELINT"].raw == ELINT
    assert stored["ELINT"].location_text == "N6919 W11919"
    assert "N6919 W11919" in stored["SITREP"].raw
    assert stored["SITREP"].location_text == "N6919 W11919"
    asyncio.run(service.publish_routed())
    bodies = {room: text for room, text in publisher.sent}
    elint = bodies["room-elint-publish"]
    assert "11WMS0869991399" in elint
    assert "N6919" not in elint
    assert elint.count("//") == ELINT.count("//")
    assert elint.endswith("SINGLE SOURCE POSS LOW")
    sitrep = bodies["room-sitrep-publish"]
    assert "69.31667N 119.31667W" in sitrep
    assert "N6919" not in sitrep
    assert sitrep.endswith("SINGLE SOURCE POSS LOW")
    adhoc = bodies["room-adhoc-publish"]
    assert "69.31667N 119.31667W" in adhoc
    assert "N6919" not in adhoc
    assert ", 120" in adhoc
    assert service._messages[0].raw == ELINT


def test_override_persists_is_on_health_and_reverts(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    publisher = _Publisher()
    service = AnalyticsService(
        _settings(
            reference_dir=str(tmp_path),
            output_coord_format="latlon",
            output_coord_configured=True,
        ),
        None,
        publisher=publisher,
    )
    health = service.health()
    assert health["output_coordinates"]["format"] == "latlon"
    assert health["output_coordinates"]["source"] == "env"
    service.save_coord_card("coord-card.yaml", CARD.read_bytes())
    assert service.output_coordinates_document()["effective"]["source"] == "coord_card"
    caplog.clear()
    updated = service.set_output_coordinates(
        {"coordinate_format": "latlon", "latlon_hemisphere": "signed", "latlon_decimals": 4}
    )
    assert updated["effective"]["format"] == "latlon"
    assert updated["effective"]["source"] == "override"
    assert updated["effective"]["hemisphere"] == "signed"
    assert "override set to latlon" in caplog.text
    reloaded = AnalyticsService(_settings(reference_dir=str(tmp_path)), None)
    assert reloaded.output_coordinates_document()["effective"]["decimals"] == 4
    assert reloaded.output_coordinates_document()["effective"]["source"] == "override"
    policy = AuthPolicy(read_token="read", write_token="write")
    denied = route_request("GET", "/v1/settings/output-coordinates", b"", {}, service, auth=policy)
    assert denied[0] == 401
    opened = route_request("GET", "/health", b"", {}, service, auth=policy)
    assert opened[0] == 200
    assert opened[1]["output_coordinates"]["source"] == "override"
    read = route_request(
        "GET",
        "/v1/settings/output-coordinates",
        b"",
        {"authorization": "Bearer read"},
        service,
        auth=policy,
    )
    assert read[0] == 200
    assert read[1]["override"]["format"] == "latlon"
    blocked = route_request(
        "PUT",
        "/v1/settings/output-coordinates",
        b'{"coordinate_format":"mgrs"}',
        {"authorization": "Bearer read"},
        service,
        auth=policy,
    )
    assert blocked[0] == 401
    caplog.clear()
    posted = route_request(
        "POST",
        "/v1/settings/output-coordinates",
        b'{"coordinate_format":"ddmm"}',
        {"authorization": "Bearer write"},
        service,
        auth=policy,
    )
    assert posted[0] == 200
    assert posted[1]["effective"]["format"] == "ddmm"
    assert "not recognized as a ChatSurfer point" in caplog.text
    invalid = route_request(
        "PUT",
        "/v1/settings/output-coordinates",
        b'{"coordinate_format":"n6919"}',
        {"authorization": "Bearer write"},
        service,
        auth=policy,
    )
    assert invalid[0] == 400
    first = service.publish_output()
    assert first is not None
    assert first["output_coordinates"]["effective"]["format"] == "ddmm"
    reverted = route_request(
        "PUT",
        "/v1/settings/output-coordinates",
        b'{"use_coord_card": true}',
        {"authorization": "Bearer write"},
        service,
        auth=policy,
    )
    assert reverted[0] == 200
    assert reverted[1]["override"] is None
    assert reverted[1]["effective"]["source"] == "coord_card"
    second = service.publish_output()
    assert second is not None
    assert second["type"] == "update"
    assert second["output_coordinates"]["effective"]["format"] == "mgrs"
    service.ingest_tacrep_body(ELINT, "room-elint-ingest")
    caplog.clear()
    asyncio.run(service.publish_routed())
    assert "11WMS0869991399" in publisher.sent[-1][1]
    assert "N6919" not in publisher.sent[-1][1]
    (tmp_path / "output-coordinates.json").write_text("{", encoding="utf-8")
    caplog.clear()
    ignored = AnalyticsService(_settings(reference_dir=str(tmp_path)), None)
    assert ignored.output_coordinates_document()["override"] is None
    assert "Ignoring output coordinate override" in caplog.text
