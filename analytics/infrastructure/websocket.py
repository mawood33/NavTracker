"""WebSocket frames for the analytics stream.

The first message on a connection is a snapshot. Later messages are
diffs from ``diff_documents``. Frames sent by the server are unmasked.
"""

import asyncio
import base64
import hashlib
from collections.abc import Mapping

_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class OutputHub:
    """Fan-out for one process. Subscribers receive documents already built."""

    def __init__(self) -> None:
        self._snapshot: dict[str, object] | None = None
        self._queues: list[asyncio.Queue[Mapping[str, object]]] = []

    def latest_snapshot(self) -> dict[str, object] | None:
        return self._snapshot

    def set_snapshot(self, document: dict[str, object]) -> None:
        self._snapshot = document

    def subscribe(self) -> asyncio.Queue[Mapping[str, object]]:
        queue: asyncio.Queue[Mapping[str, object]] = asyncio.Queue()
        self._queues.append(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[Mapping[str, object]]) -> None:
        if queue in self._queues:
            self._queues.remove(queue)

    def broadcast(self, document: Mapping[str, object]) -> None:
        for queue in list(self._queues):
            queue.put_nowait(document)


def websocket_accept(key: str) -> str:
    """RFC 6455 accept value for a client ``Sec-WebSocket-Key``."""

    digest = hashlib.sha1((key.strip() + _GUID).encode("ascii")).digest()
    return base64.b64encode(digest).decode("ascii")


def handshake_response(key: str) -> bytes:
    accept = websocket_accept(key)
    text = (
        "HTTP/1.1 101 Switching Protocols\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Accept: {accept}\r\n"
        "\r\n"
    )
    return text.encode("ascii")


def encode_server_text(payload: str) -> bytes:
    return encode_server_frame(0x1, payload.encode("utf-8"))


def encode_server_frame(opcode: int, payload: bytes) -> bytes:
    header = bytearray([0x80 | (opcode & 0x0F)])
    length = len(payload)
    if length < 126:
        header.append(length)
    elif length < 65536:
        header.append(126)
        header.extend(length.to_bytes(2, "big"))
    else:
        header.append(127)
        header.extend(length.to_bytes(8, "big"))
    return bytes(header) + payload


async def read_client_frame(
    reader: asyncio.StreamReader,
    max_bytes: int = 1_048_576,
) -> str | bytes:
    """Return ``close``, ``ignore``, or a pong frame to write back."""

    try:
        head = await reader.readexactly(2)
        length = head[1] & 0x7F
        if length == 126:
            length = int.from_bytes(await reader.readexactly(2), "big")
        elif length == 127:
            length = int.from_bytes(await reader.readexactly(8), "big")
        if length > max_bytes:
            return "close"
        mask = await reader.readexactly(4) if head[1] & 0x80 else b""
        payload = await reader.readexactly(length) if length else b""
    except asyncio.IncompleteReadError:
        return "close"
    if mask:
        payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
    opcode = head[0] & 0x0F
    if opcode == 0x8:
        return "close"
    if opcode == 0x9:
        return encode_server_frame(0xA, payload)
    return "ignore"
