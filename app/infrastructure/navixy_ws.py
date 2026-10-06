import asyncio
import json
from datetime import datetime, timezone
from typing import Any

import websockets

from app.domain.entities import TrackerPosition


class NavixyWebSocketClient:
    def __init__(
        self,
        ws_url: str,
        api_key: str,
        reconnect_seconds: int = 5,
    ):
        self.ws_url = ws_url
        self.api_key = api_key
        self.reconnect_seconds = reconnect_seconds

    async def stream_positions(self, tracker_ids: list[int]):
        while True:
            try:
                async with websockets.connect(self.ws_url) as socket:
                    await socket.send(
                        json.dumps(
                            {
                                "action": "subscribe",
                                "hash": self.api_key,
                                # "trackers": tracker_ids,
                                "requests": [
                                    {
                                        "type": "state_batch",
                                        "target": {
                                            "type": "all"
                                        }
                                    },
                                    {
                                        "type": "readings_batch",
                                        "target": {
                                            "type": "all"
                                        }
                                    }
                                ]
                            }
                        )
                    )

                    async for raw_message in socket:
                        event = json.loads(raw_message)
                        position = self._parse_event(event)

                        if position:
                            yield position

            except Exception:
                await asyncio.sleep(self.reconnect_seconds)

    @staticmethod
    def _parse_event(event: dict[str, Any]) -> TrackerPosition | None:
        if event.get("type") not in (None, "location"):
            return None

        tracker_id = event.get("tracker_id")
        latitude = event.get("lat")
        longitude = event.get("lng")

        if tracker_id is None or latitude is None or longitude is None:
            return None

        timestamp_value = event.get("timestamp")
        timestamp = (
            datetime.fromisoformat(
                timestamp_value.replace("Z", "+00:00")
            )
            if timestamp_value
            else datetime.now(timezone.utc)
        )

        return TrackerPosition(
            tracker_id=int(tracker_id),
            latitude=float(latitude),
            longitude=float(longitude),
            timestamp=timestamp,
            speed=event.get("speed"),
            heading=event.get("heading"),
            altitude=event.get("altitude"),
            raw=event,
        )
