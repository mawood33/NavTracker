"""JSON coord cards and versioned schema uploads."""

import json
import logging
from pathlib import Path

import pytest
from tests.test_analytics_reports import _settings

from analytics.composition import wire_service as AnalyticsService
from analytics.domain.access import AuthPolicy
from analytics.domain.card_schema import SchemaCatalogError, catalog_from_text
from analytics.domain.coordcard import CoordCardError, detect_card_format, parse_coord_card_text
from analytics.infrastructure.http_api import route_request
from analytics.infrastructure.http_server import _UPLOAD_PATHS

_CARD = """
{
  "schema_version": "1",
  "types": [
    {"name": "IMINT", "kind": "I", "start": 100, "end": 199, "fields": ["id", "location"]}
  ]
}
"""

_VERSIONS = {
    "default_version": "1",
    "versions": {
        "1": {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "additionalProperties": True,
            "properties": {
                "schema_version": {"type": "string"},
                "types": {"type": "array"},
                "notes": {},
            },
        },
        "2": {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "additionalProperties": True,
            "required": ["exercise_name", "types"],
            "properties": {
                "schema_version": {"const": "2"},
                "exercise_name": {"type": "string"},
                "types": {"type": "array"},
            },
        },
    },
}


def test_format_uses_extension_then_content_type_then_sniff():
    assert detect_card_format("{", suffix="card.yaml", content_type="application/json") == "yaml"
    assert detect_card_format("name,kind\n", suffix="card.json") == "json"
    assert detect_card_format(_CARD, suffix="card.txt", content_type="application/json") == "json"
    assert detect_card_format(_CARD, suffix="card.bin") == "json"
    assert detect_card_format("name,kind,start,end,fields\n", suffix="notes.txt") == "csv"
    with pytest.raises(CoordCardError, match="not valid JSON"):
        parse_coord_card_text("{", suffix=".json")


def test_json_upload_and_invalid_json_keeps_the_previous_card(tmp_path: Path):
    service = AnalyticsService(_settings(reference_dir=str(tmp_path)), None)
    saved = service.save_coord_card(
        "coord-card.json",
        _CARD.encode(),
        "application/json",
    )
    assert saved["types"] == 1
    previous = (tmp_path / "coord-card.json").read_text(encoding="utf-8")
    with pytest.raises(CoordCardError, match="not valid JSON"):
        service.save_coord_card("coord-card.json", b"{", "application/json")
    assert service._coord_card.types[0].name == "IMINT"
    assert (tmp_path / "coord-card.json").read_text(encoding="utf-8") == previous
    sniffed = route_request(
        "POST",
        "/v1/coord-card",
        _CARD.encode(),
        {"content-type": "application/json", "x-filename": "exercise-card"},
        service,
    )
    assert sniffed[0] == 200
    assert service._coord_card.schema_version == "1"


def test_schema_version_selects_side_by_side_schemas():
    catalog = catalog_from_text(json.dumps(_VERSIONS))
    loose = parse_coord_card_text(_CARD, suffix=".json", schema=catalog)
    assert loose.schema_version == "1"
    missing = {
        "schema_version": "2",
        "types": [{"name": "IMINT", "kind": "I", "start": 1, "end": 2, "fields": ["id"]}],
    }
    with pytest.raises(CoordCardError, match="schema 2"):
        parse_coord_card_text(json.dumps(missing), suffix=".json", schema=catalog)
    named = dict(missing)
    named["exercise_name"] = "FAKE"
    strict = parse_coord_card_text(json.dumps(named), suffix=".json", schema=catalog)
    assert strict.schema_version == "2"
    with pytest.raises(CoordCardError, match="Unknown coord card schema_version"):
        parse_coord_card_text(
            json.dumps({"schema_version": "9", "types": missing["types"]}),
            suffix=".json",
            schema=catalog,
        )


def test_schema_upload_merges_rejects_invalid_and_keeps_a_failing_card(tmp_path: Path, caplog):
    caplog.set_level(logging.INFO)
    assert "/v1/coord-card/schema" in _UPLOAD_PATHS
    service = AnalyticsService(_settings(reference_dir=str(tmp_path)), None)
    service.save_coord_card("coord-card.json", _CARD.encode(), "application/json")
    added = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "additionalProperties": True,
        "properties": {"schema_version": {"const": "2"}, "types": {"type": "array"}},
    }
    policy = AuthPolicy(read_token="read", write_token="write")
    denied = route_request(
        "POST",
        "/v1/coord-card/schema",
        json.dumps(added).encode(),
        {"authorization": "Bearer read", "x-schema-version": "2"},
        service,
        auth=policy,
    )
    assert denied[0] == 401
    accepted = route_request(
        "POST",
        "/v1/coord-card/schema",
        json.dumps(added).encode(),
        {
            "authorization": "Bearer write",
            "content-type": "application/json",
            "x-schema-version": "2",
        },
        service,
        auth=policy,
    )
    assert accepted[0] == 200
    assert accepted[1]["versions"] == ["1", "2"]
    assert "Coord card schema changed" in caplog.text
    stored = (tmp_path / "coord-card.schema.json").read_text(encoding="utf-8")
    broken = route_request(
        "POST",
        "/v1/coord-card/schema",
        b'{"type": "nope"}',
        {"x-schema-version": "3"},
        service,
    )
    assert broken[0] == 400
    assert broken[1]["code"] == "invalid_schema"
    assert "not a valid JSON Schema" in broken[1]["error"]
    assert (tmp_path / "coord-card.schema.json").read_text(encoding="utf-8") == stored
    stricter = {
        "default_version": "1",
        "versions": {
            "1": {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "type": "object",
                "additionalProperties": True,
                "required": ["exercise_name", "types"],
                "properties": {
                    "exercise_name": {"type": "string"},
                    "types": {"type": "array"},
                },
            }
        },
    }
    with pytest.raises(SchemaCatalogError, match="does not match the new schema"):
        service.save_coord_card_schema(json.dumps(stricter).encode())
    assert (tmp_path / "coord-card.schema.json").read_text(encoding="utf-8") == stored
    assert service._schema_catalog.version_ids() == ("1", "2")


def test_schema_upload_respects_the_size_cap(tmp_path: Path):
    service = AnalyticsService(
        _settings(reference_dir=str(tmp_path), max_upload_bytes=8),
        None,
    )
    with pytest.raises(ValueError, match="upload limit"):
        service.save_coord_card_schema(b"0123456789")
