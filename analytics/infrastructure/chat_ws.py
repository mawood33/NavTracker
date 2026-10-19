"""STOMP 1.2 over the ChatSurfer native websocket.

The handshake sends the ``Api-Key`` header and the NPE client certificate.
Topics follow ChatSurfer REST API 4.24.0. SockJS is not required.
"""

import asyncio
import json
import ssl
from collections import deque
from collections.abc import Callable
from urllib.parse import quote, urlparse

import websockets

from analytics.application.ports import ChatConnectionError
from navtracker_common.chatsurfer import ChatRoomMessage, ChatSurferApiClient
from navtracker_common.stomp import StompFrame, connect_frame, decode_frames, subscribe_frame

HEARTBEAT = b"\n"
HEARTBEAT_SECONDS = 15.0


class ChatStompSubscription:
    """One connected STOMP session."""

    def __init__(
        self,
        socket: websockets.ClientConnection,
        decode: Callable[[object, str], ChatRoomMessage],
    ) -> None:
        self._socket = socket
        self._decode = decode
        self._buffer = bytearray()
        self._frames: deque[StompFrame] = deque()

    async def receive(self) -> tuple[str, object] | None:
        """Return ``("message", ChatRoomMessage)``, ``("scrub", id)``, or None."""

        while True:
            event = self._next_event()
            if event is not None:
                return event
            if self._frames and self._frames[0].command == "":
                self._frames.popleft()
                return None
            try:
                raw = await _recv(self._socket)
            except ChatConnectionError:
                raise
            except Exception as exc:
                raise ChatConnectionError("ChatSurfer websocket closed") from exc
            if raw is None:
                try:
                    await self._socket.send(HEARTBEAT)
                except Exception as exc:
                    raise ChatConnectionError("ChatSurfer heartbeat failed") from exc
                return None
            if isinstance(raw, str):
                self._buffer.extend(raw.encode("utf-8"))
            else:
                self._buffer.extend(raw)
            self._frames.extend(decode_frames(self._buffer))

    def _next_event(self) -> tuple[str, object] | None:
        while self._frames:
            frame = self._frames[0]
            if frame.command == "":
                return None
            self._frames.popleft()
            event = _event_from(frame, self._decode)
            if event is not None:
                return event
        return None

    async def close(self) -> None:
        await self._socket.close()


class ChatStompStream:
    """Connect and subscribe to each room plus the scrub topic."""

    def __init__(
        self,
        client: ChatSurferApiClient,
        rooms: tuple[str, ...],
        *,
        api_key: str,
        client_cert: str = "",
        client_key: str = "",
    ) -> None:
        self._client = client
        self._rooms = rooms
        self._api_key = api_key
        self._cert = client_cert
        self._key = client_key

    async def connect(self) -> ChatStompSubscription:
        parsed = urlparse(self._client.base_url)
        scheme = "wss" if parsed.scheme == "https" else "ws"
        host = parsed.netloc
        url = f"{scheme}://{host}/ws/connect"
        ssl_context = _ssl_context(self._cert, self._key) if scheme == "wss" else None
        try:
            socket = await websockets.connect(
                url,
                additional_headers={"Api-Key": self._api_key},
                ssl=ssl_context,
                open_timeout=15,
            )
        except Exception as exc:
            raise ChatConnectionError("ChatSurfer websocket connect failed") from exc
        try:
            await socket.send(connect_frame(host))
            await _expect_connected(socket)
            await _subscribe(socket, self._client.domain_id, self._rooms)
        except ChatConnectionError:
            await socket.close()
            raise
        except Exception as exc:
            await socket.close()
            raise ChatConnectionError("ChatSurfer STOMP subscribe failed") from exc
        spec = self._client.read_spec

        def decode(payload: object, room: str) -> ChatRoomMessage:
            return self._client._decode_room_message(payload, room, spec)

        return ChatStompSubscription(socket, decode)


def topics_for(domain_id: str, rooms: tuple[str, ...]) -> tuple[str, ...]:
    """Destinations the stream subscribes to."""

    found: list[str] = []
    domain = quote(domain_id, safe="")
    for room in rooms:
        encoded = quote(room, safe="")
        found.append(f"/topic/chat-messages-room/{domain}/{encoded}")
        found.append(f"/topic/chat-messages-geotagged-room/{domain}/{encoded}")
    found.append("/topic/chat-messages-scrubbed")
    return tuple(found)


def _event_from(
    frame: StompFrame,
    decode: Callable[[object, str], ChatRoomMessage],
) -> tuple[str, object] | None:
    if frame.command != "MESSAGE" or not frame.body:
        return None
    try:
        payload = json.loads(frame.body)
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    if "messageId" in payload and "scrubDate" in payload:
        return ("scrub", str(payload.get("messageId", "")))
    if "id" in payload and ("text" in payload or "message" in payload):
        room = payload.get("roomName", "")
        return ("message", decode(payload, room if isinstance(room, str) else ""))
    return None


async def _subscribe(
    socket: websockets.ClientConnection,
    domain_id: str,
    rooms: tuple[str, ...],
) -> None:
    for index, topic in enumerate(topics_for(domain_id, rooms)):
        await socket.send(subscribe_frame(topic, f"sub-{index}"))


async def _expect_connected(socket: websockets.ClientConnection) -> None:
    buffer = bytearray()
    try:
        raw = await asyncio.wait_for(socket.recv(), timeout=HEARTBEAT_SECONDS)
    except Exception as exc:
        raise ChatConnectionError("ChatSurfer STOMP CONNECT was not answered") from exc
    if isinstance(raw, str):
        buffer.extend(raw.encode("utf-8"))
    else:
        buffer.extend(raw)
    frames = decode_frames(buffer)
    if not frames or frames[0].command != "CONNECTED":
        raise ChatConnectionError("ChatSurfer STOMP CONNECT was rejected")


async def _recv(socket: websockets.ClientConnection) -> bytes | str | None:
    try:
        return await asyncio.wait_for(socket.recv(), timeout=HEARTBEAT_SECONDS)
    except TimeoutError:
        return None


def _ssl_context(cert: str, key: str) -> ssl.SSLContext:
    context = ssl.create_default_context()
    if cert and key:
        context.load_cert_chain(cert, key)
    return context
