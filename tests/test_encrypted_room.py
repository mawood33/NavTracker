"""Encryption required switches only the GPS truth room."""

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest
from app.application.messages import to_outbound_message
from app.domain.entities import TrackerPosition
from app.infrastructure.config import load_settings
from cryptography.fernet import Fernet
from tests.test_analytics_reports import _settings
from tests.test_chatsurfer_contract import _Http, _Response
from tests.test_settings import _scrub

from analytics.application.dispatch import (
    destination_rooms,
    ingest_room_list,
    truth_read_rooms,
    watched_chat_rooms,
)
from analytics.composition import wire_service
from analytics.domain.coordcard import CoordCard, parse_coord_card_text
from analytics.domain.publish import PublishTable
from analytics.infrastructure.config import load_analytics_settings
from navtracker_common.chatsurfer import ChatSurferApiClient
from navtracker_common.crypto import CryptoHandler
from navtracker_common.errors import PublicationError
from navtracker_common.publication import SCHEMA_VERSION, OutboundMessage

CARD = Path("config/analytics/coord_card.example.yaml")
ENCRYPTED = "freedom_flag_encrypted"
PLAIN = "freedom_flag"


def _client(
    http: _Http,
    *,
    encryption_required: bool,
    encrypted: str = ENCRYPTED,
    handler: CryptoHandler | None = None,
    nickname: str = "navtracker",
) -> ChatSurferApiClient:
    client = ChatSurferApiClient(
        "https://chat.example",
        "chat-key",
        "domain",
        PLAIN,
        encrypted,
        nickname,
        "U",
        "U",
        1_000_000,
        4000,
        encryption_required,
        crypto_handler=handler,
    )
    client.client = http  # type: ignore[assignment]
    return client


def _publish_body(http: _Http, index: int) -> dict[str, object]:
    body = http.calls[index][2]
    assert isinstance(body, dict)
    return body


def test_config_file_selects_the_encrypted_room(monkeypatch: pytest.MonkeyPatch) -> None:
    _scrub(monkeypatch)
    settings = load_settings()
    assert settings.chatsurfer_encryption_required is True
    assert settings.chatsurfer_room_name == PLAIN
    assert settings.chatsurfer_room_name_encrypted == ENCRYPTED
    assert settings.mode == "pem"


def test_env_overrides_the_config_file(monkeypatch: pytest.MonkeyPatch) -> None:
    _scrub(monkeypatch)
    monkeypatch.setenv("CHAT_SURFER_ROOM_NAME_ENCRYPTED", "from-env")
    monkeypatch.setenv("CHAT_SURFER_ENCRYPTION_REQUIRED", "true")
    settings = load_settings()
    assert settings.chatsurfer_room_name_encrypted == "from-env"
    assert settings.chatsurfer_encryption_required is True

    monkeypatch.setenv("CHAT_SURFER_ENCRYPTION_REQUIRED", "false")
    plain = load_settings()
    assert plain.chatsurfer_encryption_required is False
    assert plain.chatsurfer_room_name == PLAIN


@pytest.mark.parametrize("flag", ["1", "yes", "True", " true "])
def test_encryption_flag_accepts_common_true_values(
    monkeypatch: pytest.MonkeyPatch,
    flag: str,
) -> None:
    _scrub(monkeypatch)
    monkeypatch.setenv("CHAT_SURFER_ENCRYPTION_REQUIRED", flag)
    settings = load_settings()
    assert settings.chatsurfer_encryption_required is True
    assert settings.chatsurfer_room_name_encrypted == ENCRYPTED


def test_missing_encrypted_room_fails_startup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _scrub(monkeypatch)
    text = Path("config/development.yaml").read_text(encoding="utf-8")
    text = text.replace('  room_name_encrypted: "freedom_flag_encrypted"\n', "")
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "development.yaml").write_text(text, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    with pytest.raises(RuntimeError, match="CHAT_SURFER_ROOM_NAME_ENCRYPTED"):
        load_settings()


def test_analytics_file_and_env_and_a_missing_room(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    for name in (
        "CHAT_SURFER_ENCRYPTION_REQUIRED",
        "CHAT_SURFER_ROOM_NAME",
        "CHAT_SURFER_ROOM_NAME_ENCRYPTED",
        "APP_ENV",
        "MODE",
        "FERNET_KEY",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("CHAT_SURFER_API_KEY", "chat-key")
    monkeypatch.setenv("ENCRYPTION_CERT", "/data/certs/client/tls.crt")
    loaded = load_analytics_settings()
    assert loaded.chatsurfer_encryption_required is True
    assert loaded.chatsurfer_room_name == PLAIN
    assert loaded.chatsurfer_room_name_encrypted == ENCRYPTED

    monkeypatch.setenv("CHAT_SURFER_ROOM_NAME_ENCRYPTED", "analytics-env")
    monkeypatch.setenv("CHAT_SURFER_ENCRYPTION_REQUIRED", "1")
    overridden = load_analytics_settings()
    assert overridden.chatsurfer_room_name_encrypted == "analytics-env"
    assert overridden.chatsurfer_encryption_required is True

    monkeypatch.delenv("CHAT_SURFER_ROOM_NAME_ENCRYPTED", raising=False)
    monkeypatch.delenv("CHAT_SURFER_ENCRYPTION_REQUIRED", raising=False)
    source = Path("config/analytics/development.yaml").read_text(encoding="utf-8")
    source = source.replace('  room_name_encrypted: "freedom_flag_encrypted"\n', "")
    config_dir = tmp_path / "analytics"
    config_dir.mkdir()
    (config_dir / "development.yaml").write_text(source, encoding="utf-8")
    with pytest.raises(RuntimeError, match="CHAT_SURFER_ROOM_NAME_ENCRYPTED"):
        load_analytics_settings(config_dir)


@pytest.mark.parametrize("mode", ["pem", "ssh", "fernet"])
def test_each_crypto_mode_keeps_the_encrypted_room(
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    _scrub(monkeypatch)
    if mode == "fernet":
        monkeypatch.setenv("MODE", "fernet")
        monkeypatch.setenv("FERNET_KEY", Fernet.generate_key().decode("ascii"))
        monkeypatch.setenv("ENCRYPTION_CERT", "")
    else:
        monkeypatch.setenv("MODE", mode)
    settings = load_settings()
    assert settings.mode == mode
    assert settings.chatsurfer_encryption_required is True
    http = _Http()
    client = _client(http, encryption_required=True)
    try:
        assert client.active_room() == ENCRYPTED
        assert client.active_room(PLAIN) == ENCRYPTED
        assert client.active_room(ENCRYPTED) == ENCRYPTED
        assert client.active_room("room-tacrep-publish") == "room-tacrep-publish"
        assert client.active_room("room-adhoc-ingest") == "room-adhoc-ingest"
        assert client.active_room("room-sitrep-publish") == "room-sitrep-publish"
    finally:
        asyncio.run(client.close())


def test_publish_and_read_use_the_encrypted_room() -> None:
    http = _Http()
    http.post_result = _Response(204, None)
    http.get_result = _Response(200, {"messages": []})
    handler = CryptoHandler(mode="fernet", fernet_key=Fernet.generate_key())

    async def scenario() -> None:
        client = _client(http, encryption_required=True, handler=handler)
        await client.publish(
            OutboundMessage(
                schema_version=SCHEMA_VERSION,
                tracker_id=7,
                latitude=1.0,
                longitude=2.0,
            )
        )
        await client.publish_text("SITREP line", room_name="room-sitrep-publish")
        await client.read_room(PLAIN)
        ciphertext = handler.encrypt("card-secret")
        http.get_result = _Response(
            200,
            {
                "messages": [
                    {
                        "id": "c1",
                        "roomName": "room-sitrep-ingest",
                        "text": ciphertext,
                        "sender": "spotter",
                        "timestamp": "2026-01-01T00:00:01+00:00",
                    }
                ]
            },
        )
        card_messages = await client.read_room("room-sitrep-ingest")
        assert card_messages[0].body == ciphertext
        moment = datetime(2026, 1, 1, tzinfo=UTC)
        page = await client.search_messages(
            room_name="room-adhoc-ingest",
            start=moment,
            end=moment,
        )
        del page
        await client.close()

    asyncio.run(scenario())
    truth = _publish_body(http, 0)
    report = _publish_body(http, 1)
    assert truth["roomName"] == ENCRYPTED
    assert truth["message"] != "SITREP line"
    assert isinstance(truth["message"], str)
    assert "algorithm" in truth["message"]
    assert report["roomName"] == "room-sitrep-publish"
    assert report["message"] == "SITREP line"
    read_url = http.calls[2][1]
    assert isinstance(read_url, str)
    assert read_url.endswith(f"/api/chat/messages/domain/{ENCRYPTED}")
    report_read = http.calls[3][1]
    assert isinstance(report_read, str)
    assert report_read.endswith("/api/chat/messages/domain/room-sitrep-ingest")
    search = http.calls[4][2]
    assert isinstance(search, dict)
    assert search["roomFilter"] == {"domain": {"properties": ["room-adhoc-ingest"]}}

    plain_http = _Http()
    plain_http.post_result = _Response(204, None)

    async def plain_scenario() -> None:
        client = _client(plain_http, encryption_required=False, encrypted="")
        await client.publish_text("plain", room_name="room-sitrep-publish")
        await client.close()

    asyncio.run(plain_scenario())
    assert _publish_body(plain_http, 0)["roomName"] == "room-sitrep-publish"

    async def missing() -> None:
        client = _client(http, encryption_required=True, encrypted="", handler=handler)
        with pytest.raises(PublicationError, match="CHAT_SURFER_ROOM_NAME_ENCRYPTED"):
            await client.publish_text("nope")
        await client.publish_text("report", room_name="room-sitrep-publish")
        await client.close()

    asyncio.run(missing())
    report_after_missing = _publish_body(http, len(http.calls) - 1)
    assert report_after_missing["roomName"] == "room-sitrep-publish"
    assert report_after_missing["message"] == "report"


def test_coord_card_and_env_rooms_ignore_the_encryption_flag() -> None:
    card = parse_coord_card_text(CARD.read_text(encoding="utf-8"), suffix=".yaml")
    secured = _settings(
        chatsurfer_encryption_required=True,
        chatsurfer_room_name=PLAIN,
        chatsurfer_room_name_encrypted=ENCRYPTED,
    )
    plain = _settings(
        chatsurfer_encryption_required=False,
        chatsurfer_room_name=PLAIN,
        chatsurfer_room_name_encrypted=ENCRYPTED,
    )
    table = PublishTable()
    for name, plain_room in (
        ("SITREP", "room-sitrep-publish"),
        ("ADHOC", "room-adhoc-publish"),
        ("ELINT", "room-elint-publish"),
    ):
        assert destination_rooms(
            name,
            confidence="POSS LOW",
            cat=4,
            sources=(),
            report_room="",
            card=card,
            settings=secured,
            table=table,
        ) == (plain_room,)
        assert destination_rooms(
            name,
            confidence="POSS LOW",
            cat=4,
            sources=(),
            report_room="",
            card=card,
            settings=plain,
            table=table,
        ) == (plain_room,)
    secured_ingest = ingest_room_list(card, secured)
    plain_ingest = ingest_room_list(card, plain)
    assert secured_ingest == plain_ingest
    assert "room-sitrep-ingest" in secured_ingest
    assert ENCRYPTED not in secured_ingest
    truth_settings = _settings(
        chatsurfer_encryption_required=True,
        chatsurfer_room_name=PLAIN,
        chatsurfer_room_name_encrypted=ENCRYPTED,
        read_rooms=(),
    )
    assert truth_read_rooms(truth_settings) == (ENCRYPTED,)
    watched = watched_chat_rooms(card, truth_settings)
    assert watched[0] == ENCRYPTED
    assert "room-sitrep-ingest" in watched
    assert PLAIN not in watched

    env_settings = _settings(
        chatsurfer_encryption_required=True,
        chatsurfer_room_name_encrypted=ENCRYPTED,
        room_publish=(
            ("SITREP", ("env-sitrep",)),
            ("ADHOC", ("env-adhoc",)),
            ("TACREP", ("env-tacrep",)),
        ),
        room_ingest=(
            ("SITREP", ("env-sitrep-in",)),
            ("ADHOC", ("env-adhoc-in",)),
            ("TACREP", ("env-tacrep-in",)),
        ),
    )
    empty = CoordCard()
    expected_publish = {
        "SITREP": ("env-sitrep",),
        "ADHOC": ("env-adhoc",),
        "TACREP": ("env-tacrep",),
    }
    for name, rooms in expected_publish.items():
        assert destination_rooms(
            name,
            confidence="PROB",
            cat=2,
            sources=(),
            report_room="",
            card=empty,
            settings=env_settings,
            table=table,
        ) == rooms
    env_ingest = ingest_room_list(empty, env_settings)
    assert "env-sitrep-in" in env_ingest
    assert "env-adhoc-in" in env_ingest
    assert "env-tacrep-in" in env_ingest
    assert ENCRYPTED not in env_ingest
    assert destination_rooms(
        "SITREP",
        confidence="POSS LOW",
        cat=4,
        sources=(),
        report_room="",
        card=card,
        settings=_settings(
            chatsurfer_encryption_required=True,
            chatsurfer_room_name_encrypted="",
        ),
        table=table,
    ) == ("room-sitrep-publish",)
    with pytest.raises(RuntimeError, match="CHAT_SURFER_ROOM_NAME_ENCRYPTED"):
        truth_read_rooms(
            _settings(
                chatsurfer_encryption_required=True,
                chatsurfer_room_name=PLAIN,
                chatsurfer_room_name_encrypted="",
                read_rooms=(),
            )
        )


class _Bus:
    """In-memory ChatSurfer. A publish is the next read of that room."""

    def __init__(self) -> None:
        self.messages: list[dict[str, object]] = []
        self.calls: list[tuple[str, str, object]] = []

    async def post(self, url: str, json: object = None) -> _Response:
        self.calls.append(("POST", url, json))
        if isinstance(json, dict):
            self.messages.append(json)
        return _Response(204, None)

    async def get(self, url: str, params: dict[str, str] | None = None) -> _Response:
        self.calls.append(("GET", url, params))
        room = url.rstrip("/").rsplit("/", 1)[-1]
        found = [
            {
                "id": f"m{index}",
                "roomName": item.get("roomName", ""),
                "text": item.get("message", ""),
                "sender": item.get("nickname", ""),
                "timestamp": "2026-01-01T00:00:01+00:00",
            }
            for index, item in enumerate(self.messages, start=1)
            if item.get("roomName") == room
        ]
        return _Response(200, {"messages": found})

    async def aclose(self) -> None:
        return None


MTI = (
    "HAWK // J-401 // MILLING // BULL-1 // 12345 // "
    "N6919 W11919 // ALPHA // CAT IV // 1919Z // -"
)
SITREP = "UNCLASSIFIED\nSITREP: HAWK1, TEL at X20, N6919 W11919, 1400Z, ACTIVE\nUNCLASSIFIED\n"
ADHOC = """\
SECRET
***START ADHOC 19OCT2601***
PRI: 2
1. deny, disrupt
2. HAWK, UAS
3. TEL, TGT-9
4. N6919 W11919, 120
5. TEL, launcher
6. LTIOV 1400Z. Report (TAI) NORTH - VACANT in ALPHA
***END ADHOC 19OCT2601***
SECRET
"""


@pytest.mark.parametrize("encryption_required", [True, False])
def test_worker_gps_reaches_analytics_and_reports_stay_on_the_card(
    encryption_required: bool,
) -> None:
    truth = ENCRYPTED if encryption_required else PLAIN
    handler = CryptoHandler(mode="fernet", fernet_key=Fernet.generate_key())
    bus = _Bus()
    worker = _client(
        bus,  # type: ignore[arg-type]
        encryption_required=encryption_required,
        handler=handler,
        nickname="navtracker",
    )
    reader = _client(
        bus,  # type: ignore[arg-type]
        encryption_required=encryption_required,
        handler=handler,
        nickname="analytics",
    )
    moment = datetime(2026, 1, 1, tzinfo=UTC)

    async def scenario() -> None:
        await worker.publish(
            to_outbound_message(
                TrackerPosition(
                    tracker_id=7,
                    latitude=31.5,
                    longitude=-97.2,
                    timestamp=moment,
                )
            )
        )
        service = wire_service(
            _settings(
                chatsurfer_encryption_required=encryption_required,
                chatsurfer_room_name=PLAIN,
                chatsurfer_room_name_encrypted=ENCRYPTED,
                chatsurfer_nickname="analytics",
                read_rooms=(),
                chat_read_enabled=True,
                chat_ingest_mode="poll",
                chat_backfill_minutes=0,
            ),
            reader,
        )
        card = parse_coord_card_text(CARD.read_text(encoding="utf-8"), suffix=".yaml")
        service._coord_card = card
        fixes = await service.poll_chat()
        assert [(fix.vehicle_id, fix.latitude, fix.longitude) for fix in fixes] == [
            ("7", 31.5, -97.2)
        ]
        assert truth_read_rooms(service.settings) == (truth,)
        service.ingest_tacrep_body(MTI, "mti")
        service.ingest_tacrep_body(SITREP, "sitrep")
        service.ingest_tacrep_body(ADHOC, "adhoc")
        await service.publish_routed()
        await worker.close()
        await reader.close()

    asyncio.run(scenario())
    gps = bus.messages[0]
    assert gps["roomName"] == truth
    body = gps["message"]
    assert isinstance(body, str)
    if encryption_required:
        assert "algorithm" in body
        assert "31.5" not in body
    else:
        assert "algorithm" not in body
        assert "31.5" in body
    reports = [item for item in bus.messages[1:] if isinstance(item.get("message"), str)]
    rooms = {item["roomName"] for item in reports}
    assert "room-mti-publish" in rooms
    assert "room-sitrep-publish" in rooms
    assert "room-adhoc-publish" in rooms
    assert truth not in rooms
    for item in reports:
        text = item["message"]
        assert isinstance(text, str)
        assert "algorithm" not in text
        assert item["roomName"] in {
            "room-mti-publish",
            "room-sitrep-publish",
            "room-adhoc-publish",
        }
