"""Clairvoyant adapter.

The Clairvoyant wire protocol is not in this repository. This adapter is
a stub with the calls the backend will make once that contract is known.

Assumptions, all configurable:

* WebSocket is preferred. ``CLAIRVOYANT_MODE=poll`` never opens it.
* Subscribe payload: ``{"action": "subscribe", "layer": <layer>, "api_key": <key>}``.
* Each frame is a JSON object or ``{"positions": [ ... ]}`` with
  ``vehicle_id``, ``latitude``, ``longitude``, ``timestamp``, and
  ``speed_mps``.
* API fallback is ``GET {base}/layers/{layer}/positions`` with header
  ``X-Api-Key``. The body uses the same position shape.
* Failures raise ``ClairvoyantConnectionError`` and do not include the key.
  They are socket/API failures, not ChatSurfer publish failures.
"""

import json
import logging
from collections.abc import Awaitable, Callable, Mapping

import httpx
import websockets

from analytics.application.ports import (
    ClairvoyantConnectionError,
)
from analytics.domain.reports import Report, ReportPolicy, parse_layer_payload
from analytics.domain.tracks import GpsFix, fix_from_mapping

logger = logging.getLogger(__name__)

SUBSCRIBE_ACTION = "subscribe"
CONNECT_TIMEOUT_SECONDS = 15.0
POLL_PATH = "/layers/{layer}/positions"
REPORT_PATH = "/layers/{layer}/reports"
ReportFetch = Callable[[str, Mapping[str, str]], Awaitable[object]]


WebSocketConnect = Callable[[str], Awaitable[websockets.ClientConnection]]


def parse_clairvoyant_body(body: object, *, source: str) -> list[GpsFix]:
    """Parse one frame or an API payload. Unknown shapes yield no fixes."""

    entries: list[object]
    if isinstance(body, list):
        entries = body
    elif isinstance(body, dict) and isinstance(body.get("positions"), list):
        entries = body["positions"]
    elif isinstance(body, dict):
        entries = [body]
    else:
        return []
    fixes: list[GpsFix] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        fix = fix_from_mapping(entry, source=source, speed_unit="mps")
        if fix is not None:
            fixes.append(fix)
    return fixes


def build_subscribe_message(layer: str, api_key: str) -> dict[str, str]:
    return {"action": SUBSCRIBE_ACTION, "layer": layer, "api_key": api_key}


class ClairvoyantApiClient:
    """HTTP poll against the assumed layer endpoint."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        layer: str,
        *,
        client: httpx.AsyncClient | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.layer = layer
        self._owns_client = client is None
        self.client = client or httpx.AsyncClient(timeout=15)

    async def close(self) -> None:
        if self._owns_client:
            await self.client.aclose()

    async def poll(self) -> list[GpsFix]:
        path = POLL_PATH.format(layer=self.layer)
        try:
            response = await self.client.get(
                f"{self.base_url}{path}",
                headers={"X-Api-Key": self.api_key},
            )
            response.raise_for_status()
            body = response.json()
        except Exception as exc:
            raise ClairvoyantConnectionError(
                "Clairvoyant API request failed"
            ) from exc
        return parse_clairvoyant_body(body, source="clairvoyant:api")


class _WebSocketSubscription:
    def __init__(self, socket: websockets.ClientConnection, layer: str):
        self._socket = socket
        self._layer = layer

    async def receive(self) -> GpsFix | None:
        try:
            raw = await self._socket.recv()
        except Exception as exc:
            raise ClairvoyantConnectionError(
                "Clairvoyant websocket receive failed"
            ) from exc
        if isinstance(raw, bytes):
            text = raw.decode("utf-8", errors="replace")
        else:
            text = raw
        try:
            body = json.loads(text)
        except ValueError:
            return None
        fixes = parse_clairvoyant_body(
            body, source=f"clairvoyant:{self._layer}"
        )
        if not fixes:
            return None
        return fixes[0]

    async def close(self) -> None:
        await self._socket.close()


class ClairvoyantWebSocket:
    def __init__(
        self,
        ws_url: str,
        api_key: str,
        layer: str,
        *,
        connect: WebSocketConnect | None = None,
    ):
        self.ws_url = ws_url
        self.api_key = api_key
        self.layer = layer
        self._connect = connect or websockets.connect

    async def connect(self) -> _WebSocketSubscription:
        logger.info("Connecting Clairvoyant websocket layer=%s", self.layer)
        try:
            socket = await self._open()
            await socket.send(
                json.dumps(build_subscribe_message(self.layer, self.api_key))
            )
        except Exception as exc:
            logger.warning("Clairvoyant websocket connect failed layer=%s", self.layer)
            raise ClairvoyantConnectionError(
                "Clairvoyant websocket connect failed"
            ) from exc
        return _WebSocketSubscription(socket, self.layer)

    async def _open(self) -> websockets.ClientConnection:
        if self._connect is websockets.connect:
            return await websockets.connect(
                self.ws_url,
                open_timeout=CONNECT_TIMEOUT_SECONDS,
            )
        return await self._connect(self.ws_url)


class ClairvoyantReportClient:
    """Read assumed report layers. Construction does not open a connection.

    ``GET {base}/layers/{layer}/reports`` with header ``X-Api-Key``.
    The body is an object, a list, or ``{"reports": [...]}``.
    """

    def __init__(
        self,
        base_url: str,
        api_key: str,
        layers: tuple[str, ...],
        policy: ReportPolicy | None = None,
        *,
        fetch: ReportFetch | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.layers = layers
        self.policy = policy or ReportPolicy()
        self._fetch = fetch
        self._client: httpx.AsyncClient | None = None

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def poll(self) -> list[Report]:
        if not self.api_key.strip():
            raise ClairvoyantConnectionError("Clairvoyant API key is required")
        found: list[Report] = []
        for layer in self.layers:
            url = f"{self.base_url}{REPORT_PATH.format(layer=layer)}"
            try:
                if self._fetch is None:
                    body = await self._http_get(url, {"X-Api-Key": self.api_key})
                else:
                    body = await self._fetch(url, {"X-Api-Key": self.api_key})
            except ClairvoyantConnectionError:
                raise
            except Exception as exc:
                raise ClairvoyantConnectionError(
                    "Clairvoyant report request failed"
                ) from exc
            found.extend(parse_layer_payload(body, layer=layer, policy=self.policy))
        return found

    async def _http_get(self, url: str, headers: Mapping[str, str]) -> object:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=15)
        try:
            response = await self._client.get(url, headers=dict(headers))
            response.raise_for_status()
            body = response.json()
        except Exception as exc:
            raise ClairvoyantConnectionError(
                "Clairvoyant report request failed"
            ) from exc
        if isinstance(body, (dict, list)):
            return body
        raise ClairvoyantConnectionError("Clairvoyant report payload was not JSON")
