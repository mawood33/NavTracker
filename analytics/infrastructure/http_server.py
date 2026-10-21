import asyncio
import json
import logging
from collections.abc import Callable
from functools import partial
from urllib.parse import parse_qs

from analytics.application.service import AnalyticsService
from analytics.domain.access import authorize, origin_allowed
from analytics.infrastructure.config import AnalyticsSettings
from analytics.infrastructure.http_api import encode_response, route_request
from analytics.infrastructure.websocket import (
    encode_server_text,
    handshake_response,
    read_client_frame,
)

logger = logging.getLogger(__name__)

_UPLOAD_PATHS = {"/reference/upload", "/v1/coord-card", "/v1/coord-card/schema"}
_HEADER_TIMEOUT_SECONDS = 30.0
_BODY_TIMEOUT_SECONDS = 60.0


async def serve(
    settings: AnalyticsSettings,
    service: AnalyticsService,
    on_listening: Callable[[int], None] | None = None,
) -> None:
    """Bind the JSON API and the ``/ws`` stream. Call this only from main."""

    async def client(
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        try:
            request = await _request(
                reader,
                max_body=settings.max_body_bytes,
                max_upload=settings.max_upload_bytes,
            )
            if request is None:
                writer.write(encode_response(400, {"error": "empty request"}))
                await writer.drain()
                return
            method, path, body, headers = request
            if _websocket_upgrade(method, path, headers):
                await _serve_websocket(reader, writer, path, headers, service)
                return
            status, payload, content_type = await asyncio.to_thread(
                partial(
                    route_request,
                    method,
                    path,
                    body,
                    headers,
                    service,
                    auth=service.auth_policy(),
                )
            )
            writer.write(encode_response(status, payload, content_type))
            await writer.drain()
        except asyncio.CancelledError:
            raise
        except BodyTooLarge:
            writer.write(encode_response(413, {"error": "request body is too large"}))
            await writer.drain()
        except LengthRequired:
            writer.write(encode_response(411, {"error": "content length is required"}))
            await writer.drain()
        except RequestTimeout:
            writer.write(encode_response(408, {"error": "request timed out"}))
            await writer.drain()
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
    _announce(server, on_listening)
    async with server:
        await server.serve_forever()


class BodyTooLarge(Exception):
    """The declared or actual body is over the configured cap."""


class LengthRequired(Exception):
    """A chunked body arrived without a length this server will wait on."""


class RequestTimeout(Exception):
    """The peer stalled while the request was being read."""


def _announce(server: asyncio.Server, on_listening: Callable[[int], None] | None) -> None:
    sockets = server.sockets
    if not sockets:
        logger.info("Analytics HTTP listening")
        return
    bound = sockets[0].getsockname()
    host = str(bound[0])
    port = int(bound[1])
    logger.info("Analytics HTTP listening on %s:%s", host, port)
    if on_listening is not None:
        on_listening(port)


async def _serve_websocket(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    path: str,
    headers: dict[str, str],
    service: AnalyticsService,
) -> None:
    policy = service.auth_policy()
    _path, _, query = path.partition("?")
    params = {key: values[-1] for key, values in _query(query).items()}
    if not origin_allowed(policy, headers):
        writer.write(encode_response(403, {"error": "origin is not allowed", "code": "origin"}))
        await writer.drain()
        return
    if authorize(policy, "GET", "/ws", headers, params) is not None:
        writer.write(encode_response(401, {"error": "unauthorized", "code": "unauthorized"}))
        await writer.drain()
        return
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
    queue = service.hub.subscribe(service.settings.max_ws_queue)
    closed = asyncio.Event()

    async def _read_client() -> None:
        while not closed.is_set():
            frame = await read_client_frame(reader, service.settings.max_ws_bytes)
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
            dropped = asyncio.create_task(queue.dropped.wait())
            done, pending = await asyncio.wait(
                {incoming, stopping, dropped},
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
            if dropped in done or queue.dropped.is_set():
                closed.set()
                break
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


def _query(query: str) -> dict[str, list[str]]:
    return parse_qs(query)


async def _request(
    reader: asyncio.StreamReader,
    *,
    max_body: int,
    max_upload: int,
) -> tuple[str, str, bytes, dict[str, str]] | None:
    try:
        head = await asyncio.wait_for(
            reader.readuntil(b"\r\n\r\n"),
            timeout=_HEADER_TIMEOUT_SECONDS,
        )
    except TimeoutError as exc:
        raise RequestTimeout from exc
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
    target = parts[1].split("?", 1)[0].rstrip("/") or "/"
    cap = max_upload if target in _UPLOAD_PATHS else max_body
    if "chunked" in headers.get("transfer-encoding", "").lower():
        raise LengthRequired
    raw_length = headers.get("content-length")
    if raw_length is None or raw_length == "":
        # HTTP requests with no length and no chunked encoding have an empty
        # body. Waiting for the client to close would deadlock a GET /health:
        # the client is waiting for the status line.
        body = b""
    else:
        try:
            length = int(raw_length)
        except ValueError:
            raise BodyTooLarge from None
        if length < 0 or length > cap:
            raise BodyTooLarge
        try:
            body = (
                await asyncio.wait_for(reader.readexactly(length), timeout=_BODY_TIMEOUT_SECONDS)
                if length
                else b""
            )
        except TimeoutError as exc:
            raise RequestTimeout from exc
        except asyncio.IncompleteReadError as exc:
            raise RequestTimeout from exc
    return parts[0], parts[1], body, headers
