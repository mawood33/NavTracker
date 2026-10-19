"""Zip caps, reference uploads, Mapbox tokens, queues, and confirmed review behavior."""

import asyncio
import io
import logging
import threading
import zipfile
from datetime import UTC, datetime, timedelta

import pytest
from tests.test_analytics_reports import _settings

from analytics.composition import wire_service as AnalyticsService
from analytics.domain.correlation import dedupe_messages, parse_tacrep_line
from analytics.domain.outputs import basemap_document
from analytics.domain.reference import ArchiveLimits
from analytics.domain.tracks import GpsFix
from analytics.infrastructure.config import load_analytics_settings
from analytics.infrastructure.reference_parse import (
    _read_member,
    load_reference_dir,
    parse_kmz,
    parse_xlsx_sites,
)
from analytics.infrastructure.websocket import OutputHub

KML = """<?xml version="1.0"?>
<kml xmlns="http://www.opengis.net/kml/2.2"><Document>
<Placemark><name>06001</name><Point><coordinates>-98,39,0</coordinates></Point></Placemark>
</Document></kml>"""
T0 = datetime(2026, 1, 1, tzinfo=UTC)


def _zip(members: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, body in members.items():
            archive.writestr(name, body)
    return buffer.getvalue()


def test_kmz_and_xlsx_reject_entry_count_and_declared_size(monkeypatch):
    crowded = _zip({f"{index}.kml": KML.encode() for index in range(3)})
    with pytest.raises(ValueError, match="entries"):
        parse_kmz(crowded, limits=ArchiveLimits(max_entries=2, max_bytes=10**6))
    with pytest.raises(ValueError, match="entries"):
        parse_xlsx_sites(
            _zip({f"xl/{index}.xml": b"<x/>" for index in range(3)}),
            limits=ArchiveLimits(max_entries=2, max_bytes=10**6),
        )
    real = zipfile.ZipFile.infolist

    def huge(self):
        infos = real(self)
        for info in infos:
            info.file_size = 10**12
        return infos

    payload = _zip({"doc.kml": KML.encode()})
    monkeypatch.setattr(zipfile.ZipFile, "infolist", huge)
    opened: list[str] = []
    real_open = zipfile.ZipFile.open

    def spy(self, name, *args, **kwargs):
        opened.append(str(name))
        return real_open(self, name, *args, **kwargs)

    monkeypatch.setattr(zipfile.ZipFile, "open", spy)
    with pytest.raises(ValueError, match="declared"):
        parse_kmz(payload, limits=ArchiveLimits(4, 1000))
    assert opened == []


def test_streaming_stops_when_decompressed_bytes_pass_the_cap():
    class _Info:
        file_size = 10
        filename = "doc.kml"

    class _Handle:
        def read(self, _size: int) -> bytes:
            return b"x" * 80

        def __enter__(self) -> "_Handle":
            return self

        def __exit__(self, *_args: object) -> bool:
            return False

    class _Archive:
        def open(self, _info: object, _mode: str = "r") -> _Handle:
            return _Handle()

    with pytest.raises(ValueError, match="decompressed"):
        _read_member(_Archive(), _Info(), ArchiveLimits(4, 20))  # type: ignore[arg-type]


def test_reference_upload_replaces_the_same_name_and_skips_bad_files(tmp_path, caplog):
    service = AnalyticsService(_settings(reference_dir=str(tmp_path)), None)
    first = service.save_upload("sites.kml", KML.encode())
    assert first == {"sites": 1, "msrs": 0}
    moved = KML.replace("-98,39,0", "-97,38,0")
    second = service.save_upload("sites.kml", moved.encode())
    assert second == {"sites": 1, "msrs": 0}
    assert service.sites()[0].longitude == -97
    before = (tmp_path / "sites.kml").read_bytes()
    with pytest.raises(ValueError):
        service.save_upload("sites.kml", b"not a reference file")
    assert (tmp_path / "sites.kml").read_bytes() == before
    assert len(service.sites()) == 1
    (tmp_path / "broken.kml").write_text("this is not xml", encoding="utf-8")
    with caplog.at_level(logging.ERROR):
        loaded = load_reference_dir(tmp_path)
    assert len(loaded.sites) == 1
    assert "broken.kml" in caplog.text


def test_public_and_server_mapbox_tokens_do_not_cross(monkeypatch):
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("CHAT_SURFER_API_KEY", "dummy")
    for name in (
        "MAPBOX_PUBLIC_TOKEN",
        "NEXT_PUBLIC_MAPBOX_TOKEN",
        "MAPBOX_SERVER_TOKEN",
        "ANALYTICS_MAPBOX_TOKEN",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("NEXT_PUBLIC_MAP_PROVIDER", "leaflet")
    monkeypatch.setenv("ROUTING_PROVIDER", "mapbox")
    monkeypatch.setenv("MAPBOX_PUBLIC_TOKEN", "public-token")
    with pytest.raises(RuntimeError, match="MAPBOX_SERVER_TOKEN"):
        load_analytics_settings()
    monkeypatch.setenv("MAPBOX_SERVER_TOKEN", "server-token")
    monkeypatch.setenv("NEXT_PUBLIC_MAP_PROVIDER", "mapbox")
    monkeypatch.delenv("MAPBOX_PUBLIC_TOKEN", raising=False)
    with pytest.raises(RuntimeError, match="MAPBOX_PUBLIC_TOKEN"):
        load_analytics_settings()
    monkeypatch.setenv("MAPBOX_PUBLIC_TOKEN", "public-token")
    settings = load_analytics_settings()
    assert settings.map_mapbox_token == "public-token"
    assert settings.routing_mapbox_token == "server-token"
    document = basemap_document(
        "mapbox",
        "",
        "",
        0,
        19,
        mapbox_token=settings.map_mapbox_token,
    )
    rendered = str(document)
    assert "public-token" in rendered
    assert "server-token" not in rendered


def test_a_full_websocket_queue_marks_the_slow_consumer_dropped():
    hub = OutputHub()
    queue = hub.subscribe(maxsize=1)
    hub.broadcast({"type": "one"})
    hub.broadcast({"type": "two"})
    assert queue.dropped.is_set()
    assert asyncio.run(queue.get())["type"] == "one"


def test_fix_retention_is_per_vehicle_and_overrides_are_pruned():
    service = AnalyticsService(_settings(fix_retention_seconds=60), None)
    service.add_fixes(
        [
            GpsFix("truck", 1, 2, T0),
            GpsFix("truck", 1, 2, T0 + timedelta(minutes=2)),
            GpsFix("other", 3, 4, T0),
        ]
    )
    assert [fix.vehicle_id for fix in service._fixes] == ["truck", "other"]
    service._activity_overrides["gone"] = None  # type: ignore[assignment]
    service._refresh_correlations()
    assert service._activity_overrides == {}


def test_batch_ingest_correlates_once(monkeypatch):
    service = AnalyticsService(_settings(), None)
    calls: list[int] = []
    monkeypatch.setattr(service, "_refresh_correlations", lambda: calls.append(1))
    body = "J-1//2026-01-01T00:00:00Z//10,20//subject=unit"
    service.ingest_batch({"events": [body, body, body], "source_id": "room"}, "")
    assert calls == [1]


def test_tacrep_identity_is_source_plus_base_id_and_not_the_message_id():
    first = parse_tacrep_line(
        "I-103//2026-01-01T00:00:00Z//10,20//id=I-103",
        source_id="alpha",
    )
    later = parse_tacrep_line(
        "I-103//2026-01-02T00:00:00Z//10,20//id=I-103",
        source_id="alpha",
    )
    other = parse_tacrep_line(
        "I-103//2026-01-01T00:00:00Z//10,20//id=I-103",
        source_id="beta",
    )
    assert first is not None and later is not None and other is not None
    assert dedupe_messages((first, later, other)).__len__() == 3
    malformed = parse_tacrep_line(
        "I-1//2026-01-01T00:00:00Z//10,20//id=chat-message-9",
        source_id="alpha",
    )
    assert malformed is not None
    assert malformed.subject == ""
    assert malformed.source_message_id != "chat-message-9"
    assert len(malformed.source_message_id) == 32


def test_analyze_and_route_requests_run_off_the_event_loop():
    service = AnalyticsService(_settings(analysis_interval_seconds=3600), None)
    seen: list[int] = []
    loop_id = threading.get_ident()

    def _analyze() -> None:
        seen.append(threading.get_ident())

    service.analyze = _analyze  # type: ignore[method-assign]

    async def _once() -> None:
        task = asyncio.create_task(service.run_analysis_loop())
        await asyncio.sleep(0.3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(_once())
    assert seen
    assert seen[0] != loop_id
