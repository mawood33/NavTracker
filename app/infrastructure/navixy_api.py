import httpx

from app.domain.entities import TrackerPosition
from app.infrastructure.navixy_parse import parse_poll_response


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

        return parse_poll_response(response.json(), tracker_id)
