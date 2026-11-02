import asyncio
from datetime import UTC, datetime

from tests.test_analytics_reports import _settings

from analytics.composition import wire_service as AnalyticsService
from analytics.domain.export import kml_network_link
from analytics.domain.outputs import items_document, parse_datetime_interval
from analytics.domain.reference import Evidence, Site
from analytics.infrastructure.http_api import route_request
from analytics.infrastructure.websocket import (
    OutputHub,
    encode_server_text,
    read_client_frame,
    websocket_accept,
)

T0 = datetime(2026, 1, 1, tzinfo=UTC)


def test_websocket_accept_matches_rfc6455():
    assert websocket_accept("dGhlIHNhbXBsZSBub25jZQ==") == "s3pPLMBiTxaQ9kYGzzhZRbK+xOo="


def test_server_text_frame_is_unmasked():
    frame = encode_server_text("hi")
    assert frame[0] == 0x81
    assert frame[1] == 2
    assert frame[2:] == b"hi"


def test_client_ping_becomes_a_pong():
    async def _read() -> str | bytes:
        reader = asyncio.StreamReader()
        reader.feed_data(bytes([0x89, 0x80, 0, 0, 0, 0]))
        return await read_client_frame(reader)

    frame = asyncio.run(_read())
    assert isinstance(frame, bytes)
    assert frame[0] == 0x8A


def test_hub_broadcasts_to_subscribers_and_keeps_the_snapshot():
    hub = OutputHub()
    queue = hub.subscribe()
    hub.set_snapshot({"type": "snapshot"})
    hub.broadcast({"type": "update"})
    assert hub.latest_snapshot() == {"type": "snapshot"}
    assert asyncio.run(queue.get())["type"] == "update"


def test_datetime_filter_keeps_evidence_inside_the_interval():
    early = Evidence(kind="stop", weight=0.05, detail="early", observed_at=T0)
    late = Evidence(
        kind="stop",
        weight=0.05,
        detail="late",
        observed_at=datetime(2026, 2, 1, tzinfo=UTC),
    )
    site = Site(
        name="06001",
        latitude=10.0,
        longitude=20.0,
        evidence=(early, late),
    )
    interval = parse_datetime_interval("2026-01-01T00:00:00Z/2026-01-02T00:00:00Z")
    document = items_document("evidence", [site], [], [], {}, None, interval)
    details = [feature["properties"]["detail"] for feature in document["features"]]  # type: ignore[index]
    assert details == ["early"]


def test_network_link_points_at_the_kml_document():
    status, payload, content_type = route_request(
        "GET",
        "/kml/networklink",
        b"",
        {},
        _Public(),
    )
    assert status == 200
    assert content_type == "application/vnd.google-earth.kml+xml"
    assert payload == kml_network_link("https://analytics.example/kml")
    assert "NetworkLink" in str(payload)


def test_analysis_publishes_a_snapshot_once_when_nothing_changes():
    service = AnalyticsService(_settings())
    service.analyze()
    first = service.publish_output()
    second = service.publish_output()
    assert first is not None and first["type"] == "snapshot"
    assert second is None
    assert first["schema"] == "navtracker.analytics"


class _Public:
    def public_base(self) -> str:
        return "https://analytics.example"
