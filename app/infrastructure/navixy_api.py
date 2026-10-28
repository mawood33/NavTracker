import asyncio

import httpx

from app.domain.entities import TrackerPosition
from app.domain.errors import TrackerDiscoveryError
from app.infrastructure.navixy_parse import (
    parse_account_timezone,
    parse_poll_response,
    parse_tracker_list,
    tracker_source_map,
)
from navtracker_common.timeutil import set_account_timezone, zone_from_name


class NavixyApiClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        timeout_seconds: float = 15,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self._tracker_list_loaded = False
        self._source_to_tracker: dict[int, int] = {}
        self._account_zone_name: str | None = None
        self.client = httpx.AsyncClient(
            timeout=timeout_seconds,
            headers={
                "Authorization": f"NVX {api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "NVX-ISO-DateTime": "true",
            },
        )

    async def close(self) -> None:
        await self.client.aclose()

    def _json(self, fields: dict[str, object]) -> dict[str, object]:
        body: dict[str, object] = {
            "hash": self.api_key,
            "iso_datetime": True,
        }
        body.update(fields)
        return body

    async def get_position(self, tracker_id: int) -> TrackerPosition:
        response = await self.client.post(
            f"{self.base_url}/tracker/get_state",
            json=self._json({"tracker_id": tracker_id}),
        )
        response.raise_for_status()

        return parse_poll_response(response.json(), tracker_id)

    async def load_account_timezone(self) -> str:
        """Read ``user_info.time_zone`` once and cache the IANA name.

        Later Navixy values with no offset are read in this zone.
        """

        if self._account_zone_name is not None:
            return self._account_zone_name
        response = await self.client.post(
            f"{self.base_url}/user/get_info",
            json=self._json({}),
        )
        response.raise_for_status()
        name = parse_account_timezone(response.json())
        if name is None:
            raise RuntimeError("Navixy user info has no time_zone")
        set_account_timezone(
            zone_from_name(name, source="user_info.time_zone")
        )
        self._account_zone_name = name
        return name

    async def list_tracker_ids(self) -> list[int]:
        """Read tracker ids from ``tracker/list``. Does not subscribe."""

        try:
            response = await self.client.post(
                f"{self.base_url}/tracker/list",
                json=self._json({}),
            )
            response.raise_for_status()
            payload = response.json()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise TrackerDiscoveryError(
                "Navixy tracker list request failed"
            ) from exc
        ids = parse_tracker_list(payload)
        self._source_to_tracker = tracker_source_map(payload)
        self._tracker_list_loaded = True
        return ids

    async def source_to_tracker(self) -> dict[int, int]:
        """Map websocket source ids to tracker ids.

        Uses the last ``tracker/list`` body. Reads the list when this
        process has not loaded one yet. The result does not change which
        tracker ids are in scope.
        """

        if not self._tracker_list_loaded:
            await self.list_tracker_ids()
        return dict(self._source_to_tracker)
