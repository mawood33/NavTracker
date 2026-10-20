"""ChatSurfer 4.24 read, search, STOMP, and scrub behavior."""

import asyncio
import json
from datetime import UTC, datetime

import httpx
import pytest
import websockets
from tests.test_analytics_reports import TEXT, _settings

from analytics.application.chat_failover import ChatFailover, run_chat_ingest
from analytics.application.ports import ChatConnectionError
from analytics.composition import wire_service as AnalyticsService
from analytics.domain.chat_text import prepare_body, skip_message
from analytics.infrastructure.chat_ws import ChatStompStream, topics_for
from navtracker_common.chatsurfer import ChatReadError, ChatRoomMessage, ChatSurferApiClient
from navtracker_common.stomp import decode_frames, encode_frame

T0 = datetime(2026, 1, 1, tzinfo=UTC)


class _Response:
    def __init__(
        self,
        status: int,
        body: object | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status_code = status
        self._body = body
        self.headers = headers or {}
        self.is_success = 200 <= status < 300
        if body is None:
            self.content = b""
        else:
            self.content = json.dumps(body).encode()

    def json(self) -> object:
        if self._body is None:
            raise ValueError("empty")
        return self._body


class _Http:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, object]] = []
        self.get_result: _Response | None = None
        self.post_result: _Response | None = None
        self.posts: list[_Response] = []

    async def get(self, url: str, params: dict[str, str] | None = None) -> _Response:
        self.calls.append(("GET", url, params))
        assert self.get_result is not None
        return self.get_result

    async def post(self, url: str, json: object = None) -> _Response:
        self.calls.append(("POST", url, json))
        if self.posts:
            return self.posts.pop(0)
        if self.post_result is not None:
            return self.post_result
        return _Response(200, {"rooms": [], "domains": []})

    async def aclose(self) -> None:
        return None


def _client(http: _Http, *, high: str = "") -> ChatSurferApiClient:
    client = ChatSurferApiClient(
        "https://chatsurfer.nro.mil",
        "super-secret-chat-key",
        "chatsurferxmppunclass",
        "freedom_flag",
        "",
        "navtracker",
        "U",
        high,
        1_000_000,
        4000,
        False,
    )
    client.client = http  # type: ignore[assignment]
    return client


def test_room_read_encodes_hash_and_decodes_chat_message() -> None:
    http = _Http()
    http.get_result = _Response(
        200,
        {
            "classification": "UNCLASSIFIED",
            "messages": [
                {
                    "id": "m1",
                    "text": "Senators urged White House",
                    "sender": "Jen",
                    "roomName": "#freedom",
                    "classification": "UNCLASSIFIED",
                    "timestamp": "2020-02-09T17:43:18.379Z",
                    "tags": ["EXERCISE"],
                    "isEdited": False,
                    "revisionId": "m1",
                    "geoTags": [
                        {
                            "location": {"type": "Point", "coordinates": [-77.03, 38.89]},
                            "anchorText": "White House",
                        }
                    ],
                }
            ],
        },
        {"X-RateLimit-UserRemaining": "59", "X-RateLimit-UserLimit": "60"},
    )

    async def scenario() -> None:
        client = _client(http)
        messages = await client.read_room("#freedom")
        assert messages[0].nickname == "Jen"
        assert messages[0].body == "Senators urged White House"
        assert messages[0].classification == "UNCLASSIFIED"
        assert messages[0].tags == ("EXERCISE",)
        assert messages[0].geo_points == ((38.89, -77.03),)
        assert client.rate_remaining == 59
        url = http.calls[0][1]
        assert isinstance(url, str)
        assert url.endswith("/api/chat/messages/chatsurferxmppunclass/%23freedom")

    asyncio.run(scenario())


def test_room_404_is_empty_and_429_defers(caplog: pytest.LogCaptureFixture) -> None:
    http = _Http()
    http.get_result = _Response(
        404,
        {"classification": "U", "code": 404, "message": "Room Does Not Exist", "fieldErrors": []},
    )

    async def scenario() -> None:
        client = _client(http)
        with pytest.raises(ChatReadError) as caught:
            await client.read_room("missing")
        assert caught.value.kind == "empty"
        assert caught.value.status_code == 404
        http.get_result = _Response(
            429,
            {"code": 429, "message": "slow down"},
            {"X-RateLimit-UserReset": "12", "X-RateLimit-UserRemaining": "0"},
        )
        with pytest.raises(ChatReadError) as limited:
            await client.read_room("missing")
        assert limited.value.kind == "rate"
        assert client.rate_limiter._not_before > 0

    with caplog.at_level("DEBUG"):
        asyncio.run(scenario())
    assert "super-secret-chat-key" not in caplog.text
    assert "Traceback" not in caplog.text
    assert "empty or missing" in caplog.text


def test_incremental_page_and_search_cursor() -> None:
    http = _Http()
    http.get_result = _Response(200, {"messages": []})
    http.posts = [
        _Response(
            200,
            {
                "messages": [{"id": "a", "text": "one", "sender": "ann", "roomName": "room"}],
                "nextCursorMark": "cursor-1",
                "searchTimeFilter": {"startDateTime": "2026-01-01T00:00:00Z"},
                "total": 1,
            },
        ),
        _Response(200, {"messages": [], "nextCursorMark": "*", "total": 0}),
    ]

    async def scenario() -> None:
        client = _client(http)
        await client.read_room("room", since="m1", limit=25)
        page = http.calls[0]
        assert page[0] == "GET"
        assert str(page[1]).endswith("/api/chat/messages/m1")
        assert page[2] == {"numAfter": "25", "numBefore": "0"}
        first = await client.search_messages(room_name="room", start=T0, end=T0, limit=100)
        assert first["nextCursorMark"] == "cursor-1"
        body = http.calls[1][2]
        assert isinstance(body, dict)
        assert body["roomFilter"] == {"chatsurferxmppunclass": {"properties": ["room"]}}
        assert body["limit"] == 100
        assert "cursor" not in body
        window = first["searchTimeFilter"]
        assert isinstance(window, dict)
        await client.search_messages(
            room_name="room",
            start=T0,
            end=T0,
            cursor="cursor-1",
            time_filter=window,
        )
        second = http.calls[2][2]
        assert isinstance(second, dict)
        assert second["cursor"] == "cursor-1"
        assert second["timeFilter"] == window

    asyncio.run(scenario())


def test_publish_accepts_204_and_omits_undocumented_field() -> None:
    http = _Http()
    http.post_result = _Response(204, None)

    async def scenario() -> None:
        client = _client(http, high="")
        result = await client.publish_text("hello", room_name="freedom_flag")
        assert result["data"] is None
        body = http.calls[0][2]
        assert isinstance(body, dict)
        assert "userHighClassification" not in body
        assert body["message"] == "hello"
        client.user_high_classification = "UNCLASSIFIED//FOUO"
        await client.publish_text("again")
        sent = http.calls[1][2]
        assert isinstance(sent, dict)
        assert sent["userHighClassification"] == "UNCLASSIFIED//FOUO"

    asyncio.run(scenario())


def test_http_error_logs_code_without_the_body(caplog: pytest.LogCaptureFixture) -> None:
    response = httpx.Response(
        400,
        json={
            "classification": "U",
            "code": 400,
            "message": "The chat domain does not exist",
            "fieldErrors": [{"rejectedValue": "super-secret-chat-key"}],
        },
    )
    with caplog.at_level("ERROR"):
        with pytest.raises(Exception) as caught:
            ChatSurferApiClient._handle_response(response)
    assert "400" in str(caught.value)
    assert "super-secret-chat-key" not in str(caught.value)
    assert "super-secret-chat-key" not in caplog.text
    assert "The chat domain does not exist" in caplog.text


def test_tags_markdown_and_scrub(caplog: pytest.LogCaptureFixture) -> None:
    own = ChatRoomMessage("1", "room", "x", "navtracker", T0, tags=("SENT_BY_BOT",))
    system = ChatRoomMessage("2", "room", "x", "other", T0, tags=("SENT_BY_SYSTEM",))
    plain = ChatRoomMessage("3", "room", "x", "spotter", T0, tags=("EXERCISE", "UNKNOWN_TAG"))
    assert skip_message(own, nickname="navtracker", skip_bots=(), exercise_only=False)
    assert skip_message(system, nickname="navtracker", skip_bots=(), exercise_only=False)
    assert not skip_message(plain, nickname="navtracker", skip_bots=(), exercise_only=True)
    marked = ChatRoomMessage(
        "4",
        "room",
        "see **bold** and [link](http://example.test)",
        "spotter",
        T0,
        tags=("MARKDOWN_FORMATTED",),
    )
    assert prepare_body(marked) == "see bold and link"

    service = AnalyticsService(_settings(chat_backfill_minutes=0), None)
    queue = service.hub.subscribe()
    fixes, reports = service._ingest.consume_messages(
        "tacrep-room",
        [ChatRoomMessage("m1", "tacrep-room", TEXT, "spotter", T0)],
    )
    assert reports and reports[0].message_id == "m1"
    assert not fixes
    service._ingest.retract("m1")
    assert service._reports == ()
    event = queue.get_nowait()
    assert event["type"] == "retract"
    assert event["message_id"] == "m1"
    with caplog.at_level("INFO"):
        service._ingest.retract("m1")
    assert "scrubbed m1" in caplog.text
    assert "trucks transferred" not in caplog.text


def test_edit_replaces_the_previous_revision() -> None:
    service = AnalyticsService(_settings(), None)
    original = ChatRoomMessage("m1", "room", TEXT, "spotter", T0, revision_id="m1")
    service._ingest.consume_messages("room", [original])
    assert len(service._reports) == 1
    edited = ChatRoomMessage(
        "m1",
        "room",
        TEXT.replace("transload", "launch"),
        "spotter",
        T0,
        revision_id="m1-rev2",
        is_edited=True,
    )
    service._ingest.consume_messages("room", [edited])
    assert len(service._reports) == 1
    assert service._reports[0].site_type == "launch"


def test_topics_encode_the_room() -> None:
    assert topics_for("domain", ("#room",))[0].endswith("/domain/%23room")
    frame = encode_frame("CONNECT", {"accept-version": "1.2", "heart-beat": "15000,15000"})
    frames = decode_frames(bytearray(frame))
    assert frames[0].command == "CONNECT"
    assert frames[0].headers["heart-beat"] == "15000,15000"


def test_stomp_fake_server_delivers_a_message() -> None:
    async def scenario() -> None:
        received: list[str] = []

        async def handler(socket: websockets.ServerConnection) -> None:
            await socket.recv()
            await socket.send(b"CONNECTED\nversion:1.2\nheart-beat:15000,15000\n\n\x00")
            while len(received) < 3:
                raw = await socket.recv()
                text = raw.decode() if isinstance(raw, bytes) else raw
                received.append(text)
            body = json.dumps(
                {
                    "id": "m9",
                    "text": "hello from stomp",
                    "sender": "ann",
                    "roomName": "#room",
                    "timestamp": "2026-01-01T00:00:00Z",
                    "tags": [],
                }
            )
            await socket.send(f"MESSAGE\n\n{body}\x00".encode())

        server = await websockets.serve(handler, "127.0.0.1", 0)
        sockets = server.sockets
        assert sockets
        port = sockets[0].getsockname()[1]
        http = _Http()
        client = _client(http)
        client.base_url = f"http://127.0.0.1:{port}"
        stream = ChatStompStream(client, ("#room",), api_key="super-secret-chat-key")
        subscription = await stream.connect()
        try:
            event = await subscription.receive()
        finally:
            await subscription.close()
            server.close()
            await server.wait_closed()
        assert event is not None
        kind, payload = event
        assert kind == "message"
        assert isinstance(payload, ChatRoomMessage)
        assert payload.body == "hello from stomp"
        assert payload.nickname == "ann"
        assert "super-secret-chat-key" not in "".join(received)

    asyncio.run(scenario())


def test_websocket_failover_uses_rest_and_keeps_going() -> None:
    class _Stream:
        def __init__(self) -> None:
            self.calls = 0

        async def connect(self) -> object:
            self.calls += 1
            raise ChatConnectionError("down")

    polls = {"n": 0}

    async def poll() -> None:
        polls["n"] += 1
        if polls["n"] >= 2:
            raise asyncio.CancelledError

    async def scenario() -> None:
        with pytest.raises(asyncio.CancelledError):
            await run_chat_ingest(
                mode="websocket",
                stream=_Stream(),  # type: ignore[arg-type]
                poll=poll,
                on_message=lambda _payload: None,
                on_scrub=lambda _message_id: None,
                policy=ChatFailover(
                    max_attempts=1,
                    backoff_initial_seconds=0,
                    backoff_cap_seconds=0,
                    probe_interval_seconds=0,
                    healthy_window_seconds=0,
                ),
                poll_interval=0,
            )

    asyncio.run(scenario())
    assert polls["n"] >= 1


def test_empty_room_does_not_stop_analysis() -> None:
    http = _Http()
    http.get_result = _Response(404, {"code": 404, "message": "Room Does Not Exist"})
    service = AnalyticsService(
        _settings(
            chatsurfer_room_name="freedom_flag",
            read_rooms=(),
            chat_poll_seconds=0.01,
            analysis_interval_seconds=0.01,
            chat_ingest_mode="poll",
            chat_backfill_minutes=0,
        ),
        _client(http),
    )
    seen = {"n": 0}

    def _analyze() -> None:
        seen["n"] += 1

    service.analyze = _analyze  # type: ignore[method-assign]

    async def scenario() -> None:
        chat = asyncio.create_task(service.run_chat_loop())
        analysis = asyncio.create_task(service.run_analysis_loop())
        for _ in range(50):
            await asyncio.sleep(0.02)
            if seen["n"] and http.calls:
                break
        assert seen["n"] >= 1
        assert http.calls
        assert not chat.done()
        assert not analysis.done()
        health = service.health()
        ingest = health["ingest"]
        assert isinstance(ingest, dict)
        chat_health = ingest["chat"]
        assert isinstance(chat_health, dict)
        rooms = chat_health["rooms"]
        assert isinstance(rooms, dict)
        assert rooms["freedom_flag"] == "empty_or_missing"
        chat.cancel()
        analysis.cancel()
        await asyncio.gather(chat, analysis, return_exceptions=True)

    asyncio.run(scenario())
