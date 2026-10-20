import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import quote

import httpx

from navtracker_common.crypto import CryptoHandler
from navtracker_common.errors import PublicationError
from navtracker_common.limiter import RateLimiter
from navtracker_common.publication import OutboundMessage

logger = logging.getLogger(__name__)

# Documented in ChatSurfer REST API 4.24.0. ``{domainId}`` and ``{roomName}``
# are path parameters. The room name is URL-encoded and may start with '#'.
MESSAGES_PATH = "/api/chat/messages/{domainId}/{roomName}"
PAGE_PATH = "/api/chat/messages/{messageId}"
SEARCH_PATH = "/api/chatsearch/messages/search"
SCRUB_PATH = "/api/dataspill/messages/scrubbed"
DOMAINS_PATH = "/api/chatserver/domains"
ROOM_SEARCH_PATH = "/api/roomsearch/rooms/search"
PUBLISH_PATH = "/api/chatserver/message"


def encrypted_room_name(room_name_encrypted: str) -> str:
    """The encrypted room, or an error. The plain room is not a substitute."""

    name = room_name_encrypted.strip()
    if name:
        return name
    raise RuntimeError(
        "CHAT_SURFER_ENCRYPTION_REQUIRED is true but "
        "CHAT_SURFER_ROOM_NAME_ENCRYPTED is unset. "
        "Refusing the unencrypted CHAT_SURFER_ROOM_NAME."
    )


def select_chat_room(
    *,
    encryption_required: bool,
    room_name: str,
    room_name_encrypted: str,
    requested: str | None = None,
) -> str:
    """Choose the ChatSurfer room for one publish or read.

    Encryption uses ``room_name_encrypted`` for every caller, including a
    coord-card or env room passed in ``requested``. A missing encrypted
    room raises. Encryption off uses the requested room, then ``room_name``.
    """

    if encryption_required:
        return encrypted_room_name(room_name_encrypted)
    chosen = requested if requested is not None and requested.strip() else room_name
    text = chosen.strip()
    if text:
        return text
    raise RuntimeError("CHAT_SURFER_ROOM_NAME is unset.")


def select_chat_rooms(
    rooms: tuple[str, ...] | list[str],
    *,
    encryption_required: bool,
    room_name_encrypted: str,
) -> tuple[str, ...]:
    """Map a room list. Encryption collapses it to the one encrypted room."""

    if not encryption_required:
        return tuple(rooms)
    if not rooms:
        return ()
    return (encrypted_room_name(room_name_encrypted),)


class ChatReadError(Exception):
    """A ChatSurfer room could not be read. Not a publish failure."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        path: str = "",
        kind: str = "unavailable",
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.path = path
        self.kind = kind


@dataclass(frozen=True)
class ChatReadSpec:
    """Room-read request and response mapping.

    ``path`` defaults to the documented room route. ``{domainId}`` and
    ``{roomName}`` are substituted and URL-encoded. A path without those
    tokens is used as written. Field names default to the ChatMessage
    schema (``text``, ``sender``) and fall back to ``message`` / ``nickname``.
    """

    path: str = MESSAGES_PATH
    page_path: str = PAGE_PATH
    method: str = "get"
    room_param: str = ""
    domain_param: str = ""
    limit_param: str = ""
    since_param: str = ""
    list_key: str = "messages"
    id_field: str = "id"
    body_field: str = "text"
    nickname_field: str = "sender"
    timestamp_field: str = "timestamp"
    room_field: str = "roomName"
    num_after: int = 100


@dataclass(frozen=True)
class ChatRoomMessage:
    message_id: str
    room_name: str
    body: str
    nickname: str
    timestamp: datetime | None
    classification: str = ""
    tags: tuple[str, ...] = ()
    revision_id: str = ""
    is_edited: bool = False
    geo_points: tuple[tuple[float, float], ...] = ()


class ChatSurferApiClient:
    read_spec: ChatReadSpec
    rate_remaining: int | None
    rate_limit_total: int | None
    rate_reset_seconds: float | None

    def __init__(
        self,
        base_url: str,
        api_key: str,
        domain_id: str,
        room_name: str,
        room_name_encrypted: str,
        nickname: str,
        classification: str,
        user_high_classification: str,
        rate_limit: float,
        max_message_size: int,
        encryption_required: bool,
        crypto_handler: CryptoHandler | None = None,
        timeout_seconds: float = 15,
        client_cert: str | None = None,
        client_key: str | None = None,
        read_spec: ChatReadSpec | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.domain_id = domain_id
        self.room_name = room_name
        self.room_name_encrypted = room_name_encrypted
        self.nickname = nickname
        self.classification = classification
        self.user_high_classification = user_high_classification
        self.max_message_size = max_message_size
        self.encryption_required = encryption_required
        self.crypto_handler = crypto_handler
        self.rate_limiter = RateLimiter(rate_limit)
        self.read_spec = read_spec or ChatReadSpec()
        self.rate_remaining = None
        self.rate_limit_total = None
        self.rate_reset_seconds = None

        self.client = httpx.AsyncClient(
            timeout=timeout_seconds,
            headers={
                "Api-Key": f"{api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            cert=self._cert_tuple(client_cert, client_key),
        )

    @staticmethod
    def _cert_tuple(
        client_cert: str | None, client_key: str | None
    ) -> tuple[str, str] | None:
        if not client_cert or not client_key:
            return None
        return (client_cert, client_key)

    def active_room(self, requested: str | None = None) -> str:
        """The room this client will actually read or publish."""

        return select_chat_room(
            encryption_required=self.encryption_required,
            room_name=self.room_name,
            room_name_encrypted=self.room_name_encrypted,
            requested=requested,
        )

    async def close(self) -> None:
        await self.client.aclose()

    async def publish(self, message: OutboundMessage) -> dict[str, Any]:
        try:
            return await self._publish_message(message)
        except asyncio.CancelledError:
            raise
        except PublicationError:
            raise
        except Exception as exc:
            raise PublicationError(
                "ChatSurfer publish failed for "
                f"tracker_id={message.tracker_id}: {exc}"
            ) from exc

    async def publish_text(
        self,
        message: str,
        *,
        room_name: str | None = None,
    ) -> dict[str, Any]:
        """Publish an already-serialized document.

        ``room_name`` selects a room other than the configured default.
        The body is still encrypted when encryption is required.
        """

        try:
            return await self._post_plaintext(message, room_name=room_name)
        except asyncio.CancelledError:
            raise
        except PublicationError:
            raise
        except Exception as exc:
            raise PublicationError(f"ChatSurfer publish failed: {exc}") from exc

    async def _publish_message(
        self,
        outbound: OutboundMessage,
    ) -> dict[str, Any]:
        message = json.dumps(
            outbound.to_payload(),
            separators=(",", ":"),
        )
        return await self._post_plaintext(message)

    async def _post_plaintext(
        self,
        message: str,
        room_name: str | None = None,
    ) -> dict[str, Any]:
        try:
            selected = self.active_room(room_name)
        except RuntimeError as exc:
            raise PublicationError(str(exc)) from exc
        if self.encryption_required:
            if self.crypto_handler is None:
                raise PublicationError(
                    "Encryption is required but CryptoHandler is unavailable"
                )
            message = self.crypto_handler.encrypt(message)

        message = self._fit_message_limit(message)
        logger.debug(
            "Publish to ChatSurfer: room-%s, nickname-%s, encryption-%s",
            selected,
            self.nickname,
            self.encryption_required,
        )

        payload: dict[str, object] = {
            "classification": self.classification,
            "domainId": self.domain_id,
            "message": message,
            "nickname": self.nickname,
            "roomName": selected,
        }
        # The documented send schema has no userHighClassification.
        # Send it only when the deployment configured one.
        if self.user_high_classification.strip():
            payload["userHighClassification"] = self.user_high_classification
        response = await self._send("post", PUBLISH_PATH, json_body=payload)
        return self._handle_response(response)

    async def read_room(
        self,
        room_name: str,
        *,
        since: str | None = None,
        limit: int = 100,
    ) -> list[ChatRoomMessage]:
        """Read one room, or messages after ``since`` once an id is known.

        A 404 on the room route means the room does not exist or has no
        messages. That raises ``ChatReadError`` with ``kind="empty"`` and
        is not a failure. A 404 on the by-id page means there is nothing
        newer and returns an empty list.
        """

        spec = self.read_spec
        page_size = _page_size(limit or spec.num_after, 1000)
        try:
            room_name = self.active_room(room_name)
        except RuntimeError as exc:
            raise ChatReadError(str(exc), kind="unavailable") from exc
        if since:
            path = spec.page_path.format(messageId=quote(since, safe=""))
            response = await self._send(
                "get",
                path,
                params={"numAfter": str(page_size), "numBefore": "0"},
            )
            if response.status_code == 404:
                return []
        else:
            path = _room_path(spec.path, self.domain_id, room_name)
            response = await self._send("get", path)
            if response.status_code == 404:
                logger.debug(
                    "ChatSurfer room empty or missing path=%s room=%s",
                    path,
                    room_name,
                )
                raise ChatReadError(
                    "ChatSurfer room is empty or missing",
                    status_code=404,
                    path=path,
                    kind="empty",
                )
        self._raise_for_status(response, path)
        return self._messages_from(response, room_name, path)

    async def search_messages(
        self,
        *,
        room_name: str,
        start: datetime,
        end: datetime,
        limit: int = 100,
        cursor: str = "",
        time_filter: dict[str, object] | None = None,
        include_private: bool = True,
    ) -> dict[str, object]:
        """One page of ``POST /api/chatsearch/messages/search``."""

        page = _page_size(limit, 1000)
        try:
            room_name = self.active_room(room_name)
        except RuntimeError as exc:
            raise ChatReadError(str(exc), path=SEARCH_PATH, kind="unavailable") from exc
        window = time_filter or {
            "startDateTime": _iso(start),
            "endDateTime": _iso(end),
        }
        payload: dict[str, object] = {
            "roomFilter": {self.domain_id: {"properties": [room_name]}},
            "timeFilter": window,
            "limit": page,
            "includePrivateRooms": include_private,
            "requestGeoTags": True,
        }
        if cursor:
            payload["cursor"] = cursor
        response = await self._send("post", SEARCH_PATH, json_body=payload)
        self._raise_for_status(response, SEARCH_PATH)
        body = _json_object(response, SEARCH_PATH)
        if not isinstance(body, dict):
            return {}
        return body

    async def scrubbed_page(self, page: int = 1, size: int = 100) -> list[str]:
        """Message ids from ``GET /api/dataspill/messages/scrubbed``."""

        bounded_page = min(1000, max(1, page))
        bounded_size = min(100, max(1, size))
        response = await self._send(
            "get",
            SCRUB_PATH,
            params={"page": str(bounded_page), "size": str(bounded_size)},
        )
        self._raise_for_status(response, SCRUB_PATH)
        body = _json_object(response, SCRUB_PATH)
        found: list[str] = []
        if not isinstance(body, dict):
            return found
        raw = body.get("scrubbedMessages", [])
        if not isinstance(raw, list):
            return found
        for item in raw:
            if isinstance(item, dict) and item.get("messageId"):
                found.append(str(item["messageId"]))
        return found

    async def warn_catalog(self, rooms: tuple[str, ...]) -> None:
        """Warn when the configured domain or a room is not listed.

        A failure here does not stop ingest. The API user must already be
        a member of a private room to send to it.
        """

        try:
            response = await self._send("get", DOMAINS_PATH)
            self._raise_for_status(response, DOMAINS_PATH)
            body = _json_object(response, DOMAINS_PATH)
            domains = body.get("domains", []) if isinstance(body, dict) else []
            ids = {
                str(item.get("id"))
                for item in domains
                if isinstance(item, dict) and item.get("id")
            }
            if self.domain_id not in ids:
                logger.warning(
                    "ChatSurfer domainId is not in GET %s",
                    DOMAINS_PATH,
                )
        except ChatReadError as exc:
            logger.warning(
                "ChatSurfer domain check failed status=%s code=%s path=%s",
                exc.status_code,
                exc.kind,
                DOMAINS_PATH,
            )
        checked: list[str] = []
        for room in rooms:
            try:
                selected = self.active_room(room)
            except RuntimeError as exc:
                logger.warning("%s", exc)
                return
            if selected not in checked:
                checked.append(selected)
        for room in checked:
            try:
                response = await self._send(
                    "post",
                    ROOM_SEARCH_PATH,
                    json_body=_room_search_body(room),
                )
                if response.status_code == 404 or not response.is_success:
                    code, message = _error_fields(response)
                    logger.warning(
                        "ChatSurfer room check status=%s code=%s message=%s",
                        response.status_code,
                        code,
                        message,
                    )
                    continue
                body = _json_object(response, ROOM_SEARCH_PATH)
                names = _room_names(body)
                if room not in names and f"#{room}" not in names:
                    logger.warning("ChatSurfer room was not returned by room search")
            except ChatReadError as exc:
                logger.warning(
                    "ChatSurfer room check failed status=%s path=%s",
                    exc.status_code,
                    ROOM_SEARCH_PATH,
                )

    async def _send(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        json_body: dict[str, object] | None = None,
    ) -> httpx.Response:
        await self.rate_limiter.acquire()
        url = f"{self.base_url}{path}"
        try:
            if method == "post":
                response = await self.client.post(url, json=json_body)
            else:
                response = await self.client.get(url, params=params)
        except asyncio.CancelledError:
            raise
        except httpx.TimeoutException:
            raise ChatReadError(
                "ChatSurfer request timed out",
                path=path,
                kind="timeout",
            ) from None
        except httpx.HTTPError:
            raise ChatReadError(
                "ChatSurfer request failed",
                path=path,
                kind="unavailable",
            ) from None
        self._note_rate(response)
        if response.status_code == 429:
            reset = self.rate_reset_seconds if self.rate_reset_seconds is not None else 1.0
            self.rate_limiter.defer(reset)
            logger.warning(
                "ChatSurfer rate limit status=429 path=%s reset_seconds=%s remaining=%s",
                path,
                reset,
                self.rate_remaining,
            )
            raise ChatReadError(
                "ChatSurfer rate limit",
                status_code=429,
                path=path,
                kind="rate",
            )
        return response

    def _messages_from(
        self,
        response: httpx.Response,
        fallback_room: str,
        path: str,
    ) -> list[ChatRoomMessage]:
        body = _json_object(response, path)
        return [
            self._decode_room_message(item, fallback_room, self.read_spec)
            for item in _message_entries(body, self.read_spec.list_key, path)
        ]

    def _raise_for_status(self, response: httpx.Response, path: str) -> None:
        if response.is_success:
            return
        code, message = _error_fields(response)
        logger.error(
            "ChatSurfer request failed status=%s code=%s path=%s message=%s",
            response.status_code,
            code,
            path,
            message,
        )
        raise ChatReadError(
            f"ChatSurfer HTTP {response.status_code} code={code}",
            status_code=response.status_code,
            path=path,
            kind="http",
        )

    def _note_rate(self, response: httpx.Response) -> None:
        remaining = _header_int(response, "X-RateLimit-UserRemaining")
        limit = _header_int(response, "X-RateLimit-UserLimit")
        reset = _header_float(response, "X-RateLimit-UserReset")
        if remaining is not None:
            self.rate_remaining = remaining
        if limit is not None:
            self.rate_limit_total = limit
        if reset is not None:
            self.rate_reset_seconds = reset

    def _decode_room_message(
        self,
        item: object,
        fallback_room: str,
        spec: ChatReadSpec,
    ) -> ChatRoomMessage:
        if not isinstance(item, dict):
            raise ChatReadError(
                "ChatSurfer room message was not an object",
                path=spec.path,
                kind="unavailable",
            )
        raw_body = _first(item, spec.body_field, "text", "message")
        if not isinstance(raw_body, str):
            raw_body = json.dumps(raw_body, separators=(",", ":"))
        body = self._maybe_decrypt(raw_body)
        message_id = str(_first(item, spec.id_field, "id") or "")
        room = _first(item, spec.room_field, "roomName")
        sender = _first(item, spec.nickname_field, "sender", "nickname")
        stamp = _parse_timestamp(_first(item, spec.timestamp_field, "timestamp"))
        classification = item.get("classification", "")
        tags = item.get("tags", [])
        revision = item.get("revisionId", "")
        edited = item.get("isEdited", False)
        return ChatRoomMessage(
            message_id=message_id,
            room_name=room if isinstance(room, str) and room else fallback_room,
            body=body,
            nickname=sender if isinstance(sender, str) else "",
            timestamp=stamp,
            classification=classification if isinstance(classification, str) else "",
            tags=tuple(str(tag) for tag in tags) if isinstance(tags, list) else (),
            revision_id=str(revision) if revision else message_id,
            is_edited=edited is True,
            geo_points=_geo_points(item.get("geoTags")),
        )

    def _maybe_decrypt(self, body: str) -> str:
        if not self.encryption_required or self.crypto_handler is None:
            return body
        try:
            payload = json.loads(body)
        except ValueError:
            return body
        if not isinstance(payload, dict) or "algorithm" not in payload:
            return body
        return self.crypto_handler.decrypt(body)

    def _fit_message_limit(self, message: str) -> str:
        if len(message) <= self.max_message_size:
            return message

        raise PublicationError(
            "Message exceeds ChatSurfer message-size limit: "
            f"{len(message)} > {self.max_message_size}"
        )

    @staticmethod
    def _handle_response(response: httpx.Response) -> dict[str, Any]:
        rate = {
            "user_limit": response.headers.get("X-RateLimit-UserLimit"),
            "user_remaining": response.headers.get("X-RateLimit-UserRemaining"),
            "user_reset": response.headers.get("X-RateLimit-UserReset"),
        }
        if response.is_success and (response.status_code == 204 or not response.content):
            return {"data": None, "rate_limit": rate}
        if response.is_success:
            try:
                body: object = response.json()
            except ValueError:
                body = None
            return {"data": body, "rate_limit": rate}
        code, message = _error_fields(response)
        logger.error(
            "ChatSurfer publish failed status=%s code=%s message=%s",
            response.status_code,
            code,
            message,
        )
        raise PublicationError(
            f"ChatSurfer HTTP {response.status_code} code={code}"
        )


def _message_entries(body: object, list_key: str, path: str) -> list[object]:
    if isinstance(body, list):
        return body
    if isinstance(body, dict):
        if list_key:
            messages = body.get(list_key)
        else:
            messages = body.get("messages", body.get("data"))
        if isinstance(messages, list):
            return messages
    raise ChatReadError(
        "ChatSurfer room read did not include messages",
        path=path,
        kind="unavailable",
    )


def _room_path(template: str, domain_id: str, room_name: str) -> str:
    if "{domainId}" in template or "{roomName}" in template:
        return template.format(
            domainId=quote(domain_id, safe=""),
            roomName=quote(room_name, safe=""),
        )
    return template


def _page_size(value: int, cap: int) -> int:
    if value < 1:
        return 1
    if value > cap:
        return cap
    return value


def _iso(moment: datetime) -> str:
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _room_search_body(room_name: str) -> dict[str, object]:
    return {
        "filterCriteria": {"filterItems": []},
        "includePrivateRooms": True,
        "keywordCriteria": {
            "searchFields": ["DISPLAY_NAME"],
            "query": room_name.lstrip("#"),
        },
        "pageNumber": 0,
        "pageSize": 10,
        "sortCriteria": {
            "orders": [{"sortField": "DISPLAY_NAME", "sortDirection": "ASC"}]
        },
    }


def _room_names(body: object) -> set[str]:
    found: set[str] = set()
    if not isinstance(body, dict):
        return found
    rooms = body.get("rooms", [])
    if not isinstance(rooms, list):
        return found
    for item in rooms:
        if not isinstance(item, dict):
            continue
        for key in ("roomName", "displayName", "topic"):
            value = item.get(key)
            if isinstance(value, str) and value:
                found.add(value)
    return found


def _json_object(response: httpx.Response, path: str) -> object:
    if not response.content:
        return {}
    try:
        return response.json()
    except ValueError:
        raise ChatReadError(
            "ChatSurfer response was not JSON",
            path=path,
            kind="unavailable",
        ) from None


def _error_fields(response: httpx.Response) -> tuple[object, str]:
    if not response.content:
        return response.status_code, ""
    try:
        body = response.json()
    except ValueError:
        return response.status_code, ""
    if not isinstance(body, dict):
        return response.status_code, ""
    code = body.get("code", response.status_code)
    message = body.get("message", "")
    if not isinstance(message, str):
        message = ""
    return code, message


def _header_int(response: httpx.Response, name: str) -> int | None:
    raw = response.headers.get(name)
    if raw is None or not str(raw).strip():
        return None
    try:
        return int(str(raw).strip())
    except ValueError:
        return None


def _header_float(response: httpx.Response, name: str) -> float | None:
    raw = response.headers.get(name)
    if raw is None or not str(raw).strip():
        return None
    try:
        return float(str(raw).strip())
    except ValueError:
        return None


def _first(item: dict[str, object], *names: str) -> object:
    for name in names:
        if name and name in item:
            return item[name]
    return ""


def _geo_points(value: object) -> tuple[tuple[float, float], ...]:
    if not isinstance(value, list):
        return ()
    points: list[tuple[float, float]] = []
    for tag in value:
        if not isinstance(tag, dict):
            continue
        location = tag.get("location")
        if not isinstance(location, dict):
            continue
        coordinates = location.get("coordinates")
        if (
            isinstance(coordinates, list)
            and len(coordinates) >= 2
            and isinstance(coordinates[0], (int, float))
            and isinstance(coordinates[1], (int, float))
            and not isinstance(coordinates[0], bool)
            and not isinstance(coordinates[1], bool)
        ):
            longitude = float(coordinates[0])
            latitude = float(coordinates[1])
            points.append((latitude, longitude))
    return tuple(points)


def backfill_window(minutes: float, now: datetime) -> tuple[datetime, datetime]:
    """Search lookback ending at ``now``."""

    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    return now - timedelta(minutes=max(minutes, 0)), now


def _parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None
