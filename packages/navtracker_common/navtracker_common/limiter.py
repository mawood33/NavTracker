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
        self._not_before = 0.0
        self._lock = asyncio.Lock()

    def defer(self, seconds: float) -> None:
        """Hold the next acquire until ``seconds`` from now.

        A 429 ``X-RateLimit-UserReset`` uses this so reads, publishes,
        and searches share one quiet period.
        """

        if seconds < 0:
            seconds = 0.0
        until = time.monotonic() + seconds
        if until > self._not_before:
            self._not_before = until

    async def acquire(self) -> None:
        async with self._lock:
            now = time.monotonic()
            wait = max(0.0, self._not_before - now, self.interval - (now - self._last_request))
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_request = time.monotonic()
