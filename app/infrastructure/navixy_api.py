import asyncio

import httpx

from app.domain.entities import TrackerPosition
from app.domain.errors import TrackerDiscoveryError
from app.infrastructure.navixy_parse import (
    parse_poll_response,
    parse_tracker_list,
    tracker_source_map,
)


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
        self.client = httpx.AsyncClient(
            timeout=timeout_seconds,
            headers={
                "Authorization": f"NVX {api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )

    async def close(self) -> None:
        await self.client.aclose()

    async def get_position(self, tracker_id: int) -> TrackerPosition:
        response = await self.client.post(
            f"{self.base_url}/tracker/get_state",
            json={
                "hash": self.api_key,
                "tracker_id": tracker_id,
            },
        )
        response.raise_for_status()

        return parse_poll_response(response.json(), tracker_id)

    async def list_tracker_ids(self) -> list[int]:
        """Read tracker ids from ``tracker/list``. Does not subscribe."""

        try:
            response = await self.client.post(
                f"{self.base_url}/tracker/list",
                json={"hash": self.api_key},
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
