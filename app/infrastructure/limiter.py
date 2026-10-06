import asyncio
import time


class RateLimiter:
    """
    rate_limit is requests per minute.
    """

    def __init__(self, rate_limit: float = 60):
        if rate_limit <= 0:
            raise ValueError("rate_limit must be greater than zero")

        self.interval = 60.0 / rate_limit
        self._last_request = 0.0
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            now = time.monotonic()
            elapsed = now - self._last_request

            if elapsed < self.interval:
                await asyncio.sleep(self.interval - elapsed)

            self._last_request = time.monotonic()
