import asyncio
import json
from datetime import UTC, datetime

import httpx
import pytest

from analytics.application.failover import ClairvoyantFailover, run_clairvoyant
from analytics.domain.tracks import GpsFix
from analytics.infrastructure.clairvoyant import (
    POLL_PATH,
    ClairvoyantApiClient,
    ClairvoyantConnectionError,
    ClairvoyantWebSocket,
    build_subscribe_message,
    parse_clairvoyant_body,
)
from analytics.infrastructure.http_api import route_request
from navtracker_common.chatsurfer import ChatSurferApiClient
from navtracker_common.crypto import CryptoHandler


class _Response:
    def __init__(self, status, payload):
        self.status_code = status
        self._payload = payload
        self.text = json.dumps(payload)

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("bad", request=None, response=None)

    def json(self):
        return self._payload

    @property
    def is_success(self):
        return self.status_code < 400

    headers = {}


class _Http:
    def __init__(self, payload=None, error=None):
        self.payload = payload
        self.error = error
        self.calls = []

    async def get(self, url, headers=None):
        self.calls.append((url, headers))
        if self.error is not None:
            raise self.error
        return _Response(200, self.payload)

    async def aclose(self):
        return None


def test_clairvoyant_poll_uses_the_assumed_layer_path_and_hides_the_key():
    async def scenario():
        http = _Http(
            {
                "positions": [
                    {
                        "vehicle_id": "truck-1",
                        "latitude": 1,
                        "longitude": 2,
                        "timestamp": "2026-01-01T00:00:00+00:00",
                        "speed_mps": 3,
                    }
                ]
            }
        )
        client = ClairvoyantApiClient(
            "https://clairvoyant.example",
            "secret-key",
            "default",
            client=http,
        )
        fixes = await client.poll()
        assert fixes[0].vehicle_id == "truck-1"
        assert fixes[0].speed_mps == 3
        assert http.calls[0][0] == (
            "https://clairvoyant.example" + POLL_PATH.format(layer="default")
        )
        assert http.calls[0][1]["X-Api-Key"] == "secret-key"
        http.error = httpx.ConnectError("secret-key down")
        with pytest.raises(ClairvoyantConnectionError) as caught:
            await client.poll()
        assert "secret-key" not in str(caught.value)

    asyncio.run(scenario())


def test_clairvoyant_subscribe_message_and_frame_parser():
    assert build_subscribe_message("roads", "k") == {
        "action": "subscribe",
        "layer": "roads",
        "api_key": "k",
    }
    fixes = parse_clairvoyant_body(
        {"vehicle_id": "a", "lat": 1, "lon": 2, "timestamp": "2026-01-01T00:00:00Z"},
        source="clairvoyant:roads",
    )
    assert fixes[0].source == "clairvoyant:roads"
    assert parse_clairvoyant_body("nope", source="x") == []


class _Socket:
    def __init__(self, frames):
        self.frames = list(frames)
        self.sent = []
        self.closed = False

    async def send(self, text):
        self.sent.append(text)

    async def recv(self):
        if not self.frames:
            raise RuntimeError("closed")
        return self.frames.pop(0)

    async def close(self):
        self.closed = True


def test_clairvoyant_websocket_sends_the_subscribe_payload():
    socket = _Socket(
        [
            json.dumps(
                {
                    "vehicle_id": "a",
                    "latitude": 1,
                    "longitude": 2,
                    "timestamp": "2026-01-01T00:00:00+00:00",
                    "speed_mps": 1,
                }
            )
        ]
    )

    async def connect(url):
        assert url == "wss://clairvoyant.example/stream"
        return socket

    async def scenario():
        client = ClairvoyantWebSocket(
            "wss://clairvoyant.example/stream",
            "secret-key",
            "default",
            connect=connect,
        )
        subscription = await client.connect()
        fix = await subscription.receive()
        await subscription.close()
        assert fix is not None and fix.vehicle_id == "a"
        assert json.loads(socket.sent[0])["api_key"] == "secret-key"
        assert socket.closed is True

    asyncio.run(scenario())


class _BoomStream:
    def __init__(self):
        self.calls = 0

    async def connect(self):
        self.calls += 1
        raise ClairvoyantConnectionError("down")


class _ScriptedReader:
    def __init__(self):
        self.calls = 0

    async def poll(self):
        self.calls += 1
        return [
            GpsFix(
                vehicle_id="p",
                latitude=1,
                longitude=2,
                timestamp=datetime(2026, 1, 1, tzinfo=UTC),
            )
        ]


class _Clock:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def monotonic(self):
        return self.now

    async def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds
        await asyncio.sleep(0)


def test_websocket_failover_switches_to_poll_and_poll_mode_skips_the_socket():
    async def scenario():
        stream = _BoomStream()
        reader = _ScriptedReader()
        seen = []
        policy = ClairvoyantFailover(
            max_attempts=2,
            backoff_initial_seconds=1,
            backoff_cap_seconds=30,
            probe_interval_seconds=5,
            healthy_window_seconds=30,
        )
        task = asyncio.create_task(
            run_clairvoyant(
                mode="websocket",
                reader=reader,
                stream=stream,
                policy=policy,
                on_fixes=seen.extend,
                clock=_Clock(),
            )
        )
        for _ in range(20):
            if reader.calls:
                break
            await asyncio.sleep(0)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        assert stream.calls >= 2
        assert reader.calls >= 1
        assert seen[0].vehicle_id == "p"

        stream2 = _BoomStream()
        reader2 = _ScriptedReader()
        task2 = asyncio.create_task(
            run_clairvoyant(
                mode="poll",
                reader=reader2,
                stream=stream2,
                policy=policy,
                on_fixes=lambda fixes: None,
                clock=_Clock(),
            )
        )
        for _ in range(10):
            if reader2.calls:
                break
            await asyncio.sleep(0)
        task2.cancel()
        await asyncio.gather(task2, return_exceptions=True)
        assert stream2.calls == 0
        assert reader2.calls >= 1

    asyncio.run(scenario())


def test_chat_reader_decrypts_a_fernet_room_message():
    key = __import__("cryptography.fernet", fromlist=["Fernet"]).Fernet.generate_key()
    handler = CryptoHandler(mode="fernet", fernet_key=key)
    plain = json.dumps(
        {
            "schema_version": 1,
            "tracker_id": 7,
            "latitude": 3,
            "longitude": 4,
            "timestamp": "2026-01-01T00:00:00+00:00",
            "speed": 36,
        }
    )

    class _ChatHttp:
        def __init__(self):
            self.calls = []

        async def get(self, url, params=None):
            self.calls.append((url, params))
            return _Response(
                200,
                {
                    "messages": [
                        {
                            "id": "m1",
                            "roomName": "freedom_flag",
                            "message": handler.encrypt(plain),
                            "nickname": "navtracker",
                            "timestamp": "2026-01-01T00:00:01+00:00",
                        }
                    ]
                },
            )

        async def aclose(self):
            return None

    async def scenario():
        http = _ChatHttp()
        client = ChatSurferApiClient(
            "https://chat.example",
            "chat-key",
            "domain",
            "freedom_flag",
            "freedom_flag_encrypted",
            "analytics",
            "U",
            "U",
            60,
            4000,
            True,
            crypto_handler=handler,
        )
        await client.client.aclose()
        client.client = http
        messages = await client.read_room("freedom_flag", since="0")
        assert messages[0].body == plain
        assert messages[0].message_id == "m1"
        assert http.calls[0][0].endswith("/api/chatserver/messages")
        assert http.calls[0][1]["roomName"] == "freedom_flag"
        await client.close()

    asyncio.run(scenario())


class _Snapshot:
    def health(self):
        return {"status": "ok"}

    def events(self):
        return [{"kind": "stop"}]

    def areas(self):
        return []

    def sites(self):
        return []

    def routes(self):
        return []

    def reload_reference(self):
        return {"sites": 1, "msrs": 0}

    def save_upload(self, filename, body):
        assert filename == "sites.csv"
        assert b"Name/ID" in body
        return {"sites": 2, "msrs": 0}


def test_http_routes_events_and_upload():
    status, body, content_type = route_request("GET", "/events", b"", {}, _Snapshot())
    assert status == 200
    assert content_type == "application/json"
    assert isinstance(body, dict)
    assert body["events"][0]["kind"] == "stop"
    status, body, _content_type = route_request(
        "POST",
        "/reference/upload",
        b"Name/ID\n",
        {"x-filename": "sites.csv"},
        _Snapshot(),
    )
    assert status == 200
    assert isinstance(body, dict)
    assert body["sites"] == 2
    status, _body, _content_type = route_request(
        "POST",
        "/reference/upload",
        b"",
        {"x-filename": "../secret.csv"},
        _Snapshot(),
    )
    assert status == 400
