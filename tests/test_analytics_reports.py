import asyncio
from datetime import UTC, datetime

from analytics.application.failover import ClairvoyantFailover
from analytics.application.service import AnalyticsService
from analytics.domain.assessment import ConfidencePolicy
from analytics.domain.grid import GridPolicy
from analytics.domain.reports import ReportPolicy, dedupe_reports, parse_tacrep
from analytics.domain.thresholds import AnalysisThresholds
from analytics.infrastructure.clairvoyant import (
    ClairvoyantConnectionError,
    ClairvoyantReportClient,
)
from analytics.infrastructure.config import AnalyticsSettings
from analytics.infrastructure.reference_parse import ColumnMap
from navtracker_common.chatsurfer import ChatRoomMessage

T0 = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
TEXT = """TACREP
DTG: 011200ZJAN2026
LOC: 10.0 20.0
ACTIVITY: transload
REMARKS: trucks transferred cargo
"""


def test_text_tacrep_parses_position_and_type():
    report = parse_tacrep(TEXT, layer="tacrep", message_id="m1", received_at=T0)
    assert report is not None
    assert report.source == "chatsurfer"
    assert report.layer == "tacrep"
    assert report.message_id == "m1"
    assert report.latitude == 10.0
    assert report.longitude == 20.0
    assert report.site_type == "transload"


def test_duplicate_reports_collapse_on_message_id():
    first = parse_tacrep(TEXT, layer="tacrep", message_id="m1", received_at=T0)
    second = parse_tacrep(TEXT, layer="tacrep", message_id="m1", received_at=T0)
    assert first is not None and second is not None
    assert len(dedupe_reports((first, second))) == 1


def test_json_tacrep_without_a_position_is_ignored():
    assert (
        parse_tacrep(
            '{"text": "transload nearby"}',
            layer="tacrep",
            message_id="m2",
            received_at=T0,
        )
        is None
    )


def test_service_reads_configured_tacrep_rooms():
    class _Chat:
        def __init__(self) -> None:
            self.since: list[str | None] = []

        async def read_room(self, room: str, since: str | None = None) -> list[ChatRoomMessage]:
            assert room == "tacrep-room"
            self.since.append(since)
            if since:
                return []
            return [
                ChatRoomMessage(
                    message_id="m1",
                    room_name=room,
                    body=TEXT,
                    nickname="spotter",
                    timestamp=T0,
                )
            ]

    service = AnalyticsService(_settings(tacrep_rooms=("tacrep-room",)), _Chat())  # type: ignore[arg-type]
    found = asyncio.run(service.poll_tacrep())
    assert len(found) == 1
    assert found[0].message_id == "m1"
    again = asyncio.run(service.poll_tacrep())
    assert again == []


def test_clairvoyant_report_client_uses_the_injected_fetch():
    seen: list[str] = []

    async def fetch(url: str, headers: dict[str, str]) -> dict[str, object]:
        seen.append(url)
        assert headers["X-Api-Key"] == "secret"
        return {
            "reports": [
                {
                    "id": "r1",
                    "lat": 10,
                    "lon": 20,
                    "timestamp": "2026-01-01T12:00:00Z",
                    "text": "missile launch",
                }
            ]
        }

    client = ClairvoyantReportClient(
        "https://clairvoyant.example",
        "secret",
        ("signals",),
        ReportPolicy(),
        fetch=fetch,  # type: ignore[arg-type]
    )
    reports = asyncio.run(client.poll())
    assert seen == ["https://clairvoyant.example/layers/signals/reports"]
    assert reports[0].source == "clairvoyant"
    assert reports[0].site_type == "launch"
    assert reports[0].message_id == "r1"


def test_clairvoyant_report_client_requires_a_key():
    client = ClairvoyantReportClient("https://clairvoyant.example", "", ("signals",))

    async def _poll() -> None:
        await client.poll()

    try:
        asyncio.run(_poll())
    except ClairvoyantConnectionError as exc:
        assert "key" not in str(exc).lower() or "required" in str(exc).lower()
        assert "secret" not in str(exc)
    else:
        raise AssertionError("expected ClairvoyantConnectionError")


def _settings(**overrides: object) -> AnalyticsSettings:
    values: dict[str, object] = {
        "environment": "development",
        "http_host": "127.0.0.1",
        "http_port": 9,
        "reference_dir": "",
        "publish_events": False,
        "analysis_interval_seconds": 30.0,
        "chat_poll_seconds": 15.0,
        "chat_speed_unit": "kmh",
        "read_rooms": (),
        "chatsurfer_base_url": "https://example.test",
        "chatsurfer_api_key": "k",
        "chatsurfer_domain_id": "d",
        "chatsurfer_room_name": "r",
        "chatsurfer_room_name_encrypted": "",
        "chatsurfer_nickname": "n",
        "chatsurfer_classification": "u",
        "chatsurfer_user_high_classification": "u",
        "chatsurfer_client_cert": "",
        "chatsurfer_client_key": "",
        "chatsurfer_rate_limit": 60.0,
        "chatsurfer_max_message_size": 4000,
        "chatsurfer_encryption_required": False,
        "encryption_cert": "",
        "encryption_key": "",
        "mode": "pem",
        "fernet_key": None,
        "clairvoyant_enabled": False,
        "clairvoyant_mode": "poll",
        "clairvoyant_ws_url": "",
        "clairvoyant_base_url": "",
        "clairvoyant_api_key": "",
        "clairvoyant_layer": "default",
        "clairvoyant_failover": ClairvoyantFailover(),
        "thresholds": AnalysisThresholds(),
        "confidence": ConfidencePolicy(),
        "grid": GridPolicy(),
        "report_policy": ReportPolicy(),
        "tacrep_rooms": (),
        "clairvoyant_report_layers": (),
        "map_provider": "none",
        "map_tile_url": "",
        "map_attribution": "",
        "map_min_zoom": 0,
        "map_max_zoom": 19,
        "routing_engine": "msr",
        "routing_base_url": "",
        "routing_cache_ttl_seconds": 300.0,
        "routing_cache_size": 16,
        "routing_snap_meters": 500.0,
        "routing_join_meters": 40.0,
        "routing_profiles": (),
        "frontend_enabled": False,
        "frontend_ingest_url": "",
        "public_base_url": "",
        "columns": ColumnMap(),
    }
    values.update(overrides)
    return AnalyticsSettings(**values)  # type: ignore[arg-type]
