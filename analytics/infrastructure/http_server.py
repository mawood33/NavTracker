import asyncio
import json

from analytics.application.service import AnalyticsService
from analytics.infrastructure.config import AnalyticsSettings
from analytics.infrastructure.http_api import encode_response, route_request
from analytics.infrastructure.websocket import (
    encode_server_text,
    handshake_response,
    read_client_frame,
)


async def serve(settings: AnalyticsSettings, service: AnalyticsService) -> None:
    """Bind the JSON API and the ``/ws`` stream. Call this only from main."""

    async def client(
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        try:
            request = await _request(reader)
            if request is None:
                writer.write(encode_response(400, {"error": "empty request"}))
                await writer.drain()
                return
            method, path, body, headers = request
            if _websocket_upgrade(method, path, headers):
                await _serve_websocket(reader, writer, headers, service)
                return
            status, payload, content_type = route_request(
                method, path, body, headers, service
            )
            writer.write(encode_response(status, payload, content_type))
            await writer.drain()
        except asyncio.CancelledError:
            raise
        except Exception:
            writer.write(encode_response(500, {"error": "internal error"}))
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(
        client,
        settings.http_host,
        settings.http_port,
    )
    async with server:
        await server.serve_forever()


async def _serve_websocket(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    headers: dict[str, str],
    service: AnalyticsService,
) -> None:
    key = headers.get("sec-websocket-key", "").strip()
    if not key:
        writer.write(encode_response(400, {"error": "missing Sec-WebSocket-Key"}))
        await writer.drain()
        return
    writer.write(handshake_response(key))
    snapshot = service.hub.latest_snapshot()
    if snapshot is None:
        snapshot = service.output_snapshot()
    writer.write(encode_server_text(json.dumps(snapshot, separators=(",", ":"))))
    await writer.drain()
    queue = service.hub.subscribe()
    closed = asyncio.Event()

    async def _read_client() -> None:
        while not closed.is_set():
            frame = await read_client_frame(reader)
            if frame == "close":
                closed.set()
                return
            if isinstance(frame, bytes):
                writer.write(frame)
                await writer.drain()

    reader_task = asyncio.create_task(_read_client())
    try:
        while not closed.is_set():
            incoming = asyncio.create_task(queue.get())
            stopping = asyncio.create_task(closed.wait())
            done, pending = await asyncio.wait(
                {incoming, stopping},
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
            if incoming not in done:
                break
            document = incoming.result()
            body = json.dumps(document, separators=(",", ":"))
            writer.write(encode_server_text(body))
            await writer.drain()
    finally:
        closed.set()
        reader_task.cancel()
        service.hub.unsubscribe(queue)


def _websocket_upgrade(method: str, path: str, headers: dict[str, str]) -> bool:
    target = path.split("?", 1)[0].rstrip("/") or "/"
    upgrade = headers.get("upgrade", "").lower() == "websocket"
    return method == "GET" and target == "/ws" and upgrade


async def _request(
    reader: asyncio.StreamReader,
) -> tuple[str, str, bytes, dict[str, str]] | None:
    try:
        head = await reader.readuntil(b"\r\n\r\n")
    except asyncio.IncompleteReadError as exc:
        head = exc.partial
    if not head:
        return None
    text = head.decode("iso-8859-1", errors="replace")
    lines = text.split("\r\n")
    parts = lines[0].split(" ")
    if len(parts) < 2:
        return "GET", "/", b"", {}
    headers: dict[str, str] = {}
    for line in lines[1:]:
        if ":" not in line:
            continue
        name, value = line.split(":", 1)
        headers[name.strip().lower()] = value.strip()
    length = int(headers.get("content-length", "0") or "0")
    body = await reader.readexactly(length) if length else b""
    return parts[0], parts[1], body, headers
