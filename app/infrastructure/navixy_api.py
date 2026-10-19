from datetime import datetime
from typing import Any

import httpx

from app.domain.entities import TrackerPosition


class NavixyApiClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        timeout_seconds: float = 15,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
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

        payload: dict[str, Any] = response.json()

        if payload.get("success") is False:
            raise RuntimeError(f"Navixy API error: {payload}")

        state = payload["state"]
        gps = state["gps"]
        location = gps["location"]

        updated = gps.get("updated")
        timestamp = (
            datetime.fromisoformat(updated.replace("Z", "+00:00"))
            if updated
            else None
        )

        return TrackerPosition(
            tracker_id=tracker_id,
            latitude=float(location["lat"]),
            longitude=float(location["lng"]),
            timestamp=timestamp,
            speed=gps.get("speed"),
            heading=gps.get("heading"),
            altitude=gps.get("alt"),
            connection_status=state.get("connection_status"),
            movement_status=state.get("movement_status"),
            raw=payload,
        )
