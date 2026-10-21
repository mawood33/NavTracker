"""GET /health must answer while a source is stalled."""

import asyncio
import json
import urllib.error
import urllib.request

import pytest
from tests.test_analytics_reports import _settings

from analytics.composition import build_analytics
from analytics.infrastructure.http_server import LengthRequired, _request, serve

SECRET = "super-secret-chat-key"


def test_get_without_content_length_does_not_wait_for_the_client() -> None:
    async def scenario() -> None:
        reader = asyncio.StreamReader()
        reader.feed_data(
            b"GET /health HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n"
        )
        parsed = await asyncio.wait_for(
            _request(reader, max_body=1024, max_upload=1024),
            timeout=0.5,
        )
        assert parsed is not None
        method, path, body, _headers = parsed
        assert method == "GET"
        assert path == "/health"
        assert body == b""

    asyncio.run(scenario())


def test_content_length_still_reads_the_body() -> None:
    async def scenario() -> None:
        reader = asyncio.StreamReader()
        reader.feed_data(b"POST /v1/events HTTP/1.1\r\nContent-Length: 2\r\n\r\n{}")
        parsed = await asyncio.wait_for(
            _request(reader, max_body=1024, max_upload=1024),
            timeout=0.5,
        )
        assert parsed is not None
        assert parsed[2] == b"{}"

    asyncio.run(scenario())


def test_chunked_body_is_rejected_without_waiting() -> None:
    async def scenario() -> None:
        reader = asyncio.StreamReader()
        reader.feed_data(b"POST /v1/events HTTP/1.1\r\nTransfer-Encoding: chunked\r\n\r\n")
        with pytest.raises(LengthRequired):
            await asyncio.wait_for(
                _request(reader, max_body=1024, max_upload=1024),
                timeout=0.5,
            )

    asyncio.run(scenario())


def test_health_answers_while_a_source_is_stalled(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level("INFO")

    stop = asyncio.Event()

    async def blackhole(
        _reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        try:
            await stop.wait()
        finally:
            writer.close()
            await writer.wait_closed()

    async def scenario() -> None:
        trap = await asyncio.start_server(blackhole, "127.0.0.1", 0)
        sockets = trap.sockets or []
        trap_port = int(sockets[0].getsockname()[1])
        ready = asyncio.Event()
        ports: list[int] = []

        def listening(port: int) -> None:
            ports.append(port)
            ready.set()

        settings = _settings(
            http_host="127.0.0.1",
            http_port=0,
            chat_read_enabled=True,
            chat_ingest_mode="websocket",
            chatsurfer_base_url=f"http://127.0.0.1:{trap_port}",
            chatsurfer_api_key=SECRET,
            chatsurfer_encryption_required=False,
        )
        app = build_analytics(settings)
        server = asyncio.create_task(serve(settings, app.service, on_listening=listening))
        stalled = asyncio.create_task(asyncio.Event().wait(), name="stalled-source")
        chat = asyncio.create_task(app.service.run_chat_loop(), name="analytics-chat")

        async def catalog_started() -> None:
            while "ChatSurfer catalog check starting" not in caplog.text:
                await asyncio.sleep(0.01)

        try:
            await asyncio.wait_for(ready.wait(), timeout=2)
            body = await asyncio.wait_for(
                asyncio.to_thread(_urllib_health, ports[0]),
                timeout=2,
            )
            payload = json.loads(body)
            assert payload["status"] == "ok"
            await asyncio.wait_for(catalog_started(), timeout=2)
        finally:
            stop.set()
            chat.cancel()
            stalled.cancel()
            server.cancel()
            trap.close()
            await asyncio.wait_for(
                asyncio.gather(chat, stalled, server, return_exceptions=True),
                timeout=2,
            )
            await asyncio.wait_for(trap.wait_closed(), timeout=2)
            await asyncio.wait_for(app.aclose(), timeout=2)

    asyncio.run(scenario())
    assert "Analytics HTTP listening" in caplog.text
    assert "ChatSurfer ingest starting mode=websocket" in caplog.text
    assert SECRET not in caplog.text


def _urllib_health(port: int) -> bytes:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1) as response:
        status = response.status
        body = response.read()
    if status != 200:
        raise urllib.error.URLError(f"status {status}")
    return body
