"""Minimal STOMP 1.2 frames for the ChatSurfer websocket.

ChatSurfer expects a heart-beat at least every 15 seconds and sends its
own. A heart-beat on the wire is a single newline. Frames end with NUL.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class StompFrame:
    command: str
    headers: dict[str, str]
    body: str


def encode_frame(command: str, headers: dict[str, str], body: str = "") -> bytes:
    lines = [command]
    for key, value in headers.items():
        lines.append(f"{key}:{_escape(value)}")
    lines.append("")
    text = "\n".join(lines) + body
    return text.encode("utf-8") + b"\x00"


def connect_frame(host: str, heartbeat_ms: int = 15000) -> bytes:
    return encode_frame(
        "CONNECT",
        {
            "accept-version": "1.2",
            "host": host,
            "heart-beat": f"{heartbeat_ms},{heartbeat_ms}",
        },
    )


def subscribe_frame(destination: str, subscription_id: str) -> bytes:
    return encode_frame(
        "SUBSCRIBE",
        {"id": subscription_id, "destination": destination, "ack": "auto"},
    )


def decode_frames(buffer: bytearray) -> list[StompFrame]:
    """Pull complete frames out of ``buffer``. Heart-beats become empty commands."""

    frames: list[StompFrame] = []
    while True:
        if buffer.startswith(b"\n") or buffer.startswith(b"\r\n"):
            if buffer.startswith(b"\r\n"):
                del buffer[:2]
            else:
                del buffer[:1]
            frames.append(StompFrame("", {}, ""))
            continue
        end = buffer.find(b"\x00")
        if end < 0:
            return frames
        raw = bytes(buffer[:end])
        del buffer[: end + 1]
        frames.append(_parse_frame(raw))
    return frames


def _parse_frame(raw: bytes) -> StompFrame:
    text = raw.decode("utf-8", errors="replace")
    head, _, body = text.partition("\n\n")
    lines = head.split("\n")
    command = lines[0].strip()
    headers: dict[str, str] = {}
    for line in lines[1:]:
        if not line or ":" not in line:
            continue
        key, value = line.split(":", 1)
        headers[key] = _unescape(value)
    return StompFrame(command, headers, body)


def _escape(value: str) -> str:
    return (
        value.replace("\\", "\\\\")
        .replace("\n", "\\n")
        .replace(":", "\\c")
    )


def _unescape(value: str) -> str:
    return (
        value.replace("\\c", ":")
        .replace("\\n", "\n")
        .replace("\\\\", "\\")
    )
