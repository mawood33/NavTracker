"""Encryption required uses room_name_encrypted for every ChatSurfer room."""

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest
from app.infrastructure.config import load_settings
from cryptography.fernet import Fernet
from tests.test_analytics_reports import _settings
from tests.test_chatsurfer_contract import _Http, _Response
from tests.test_settings import _scrub

from analytics.application.dispatch import destination_rooms, ingest_room_list
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
) -> ChatSurferApiClient:
    client = ChatSurferApiClient(
        "https://chat.example",
        "chat-key",
        "domain",
        PLAIN,
        encrypted,
        "navtracker",
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
        assert client.active_room("room-tacrep-publish") == ENCRYPTED
        assert client.active_room("room-adhoc-ingest") == ENCRYPTED
        assert client.active_room("room-sitrep-publish") == ENCRYPTED
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
        moment = datetime(2026, 1, 1, tzinfo=UTC)
        page = await client.search_messages(
            room_name="room-adhoc-ingest",
            start=moment,
            end=moment,
        )
        del page
        await client.close()

    asyncio.run(scenario())
    assert _publish_body(http, 0)["roomName"] == ENCRYPTED
    assert _publish_body(http, 1)["roomName"] == ENCRYPTED
    read_url = http.calls[2][1]
    assert isinstance(read_url, str)
    assert read_url.endswith(f"/api/chat/messages/domain/{ENCRYPTED}")
    search = http.calls[3][2]
    assert isinstance(search, dict)
    assert search["roomFilter"] == {"domain": {"properties": [ENCRYPTED]}}

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
            await client.publish_text("nope", room_name=PLAIN)
        await client.close()

    asyncio.run(missing())


def test_coord_card_and_env_rooms_use_the_encrypted_room() -> None:
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
        ) == (ENCRYPTED,)
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
    assert ingest_room_list(card, secured) == (ENCRYPTED,)
    assert "room-sitrep-ingest" in ingest_room_list(card, plain)

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
    for name in ("SITREP", "ADHOC", "TACREP"):
        assert destination_rooms(
            name,
            confidence="PROB",
            cat=2,
            sources=(),
            report_room="",
            card=empty,
            settings=env_settings,
            table=table,
        ) == (ENCRYPTED,)
    assert ingest_room_list(empty, env_settings) == (ENCRYPTED,)
    with pytest.raises(RuntimeError, match="CHAT_SURFER_ROOM_NAME_ENCRYPTED"):
        destination_rooms(
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
        )
