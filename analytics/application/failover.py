"""Websocket-to-API failover for Clairvoyant.

Mirrors the ingest worker: exponential backoff, a consecutive-failure
threshold, then polling while a probe tries the socket again. Only
``ClairvoyantConnectionError`` counts. ChatSurfer errors do not.
``poll`` mode never opens the websocket.
"""

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass

from analytics.application.ports import (
    ClairvoyantConnectionError,
    ClairvoyantPoller,
    ClairvoyantStream,
)
from analytics.domain.tracks import GpsFix

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ClairvoyantFailover:
    max_attempts: int = 5
    backoff_initial_seconds: float = 1.0
    backoff_cap_seconds: float = 30.0
    probe_interval_seconds: float = 60.0
    healthy_window_seconds: float = 30.0

    def backoff_seconds(self, consecutive_failures: int) -> float:
        power = max(consecutive_failures, 1) - 1
        delay = self.backoff_initial_seconds * (1 << power)
        if delay < self.backoff_cap_seconds:
            return delay
        return self.backoff_cap_seconds


class _Clock:
    def monotonic(self) -> float:
        return asyncio.get_running_loop().time()

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


async def run_clairvoyant(
    *,
    mode: str,
    reader: ClairvoyantPoller,
    stream: ClairvoyantStream,
    policy: ClairvoyantFailover,
    on_fixes: Callable[[list[GpsFix]], None],
    clock: _Clock | None = None,
) -> None:
    """Read until cancelled. ``mode`` is ``websocket`` or ``poll``."""

    ticker = clock or _Clock()
    if mode == "poll":
        await _poll_forever(reader, on_fixes, policy, ticker)
        return
    failures = 0
    while True:
        try:
            await _consume_socket(
                stream, on_fixes, policy, ticker, require_healthy=False
            )
            failures = 0
        except ClairvoyantConnectionError as exc:
            failures += 1
            logger.warning(
                "Clairvoyant websocket failure attempt=%s: %s",
                failures,
                exc,
            )
            if failures >= policy.max_attempts:
                logger.info(
                    "Ingestion mode switch: websocket -> poll after %s "
                    "consecutive Clairvoyant socket failures",
                    failures,
                )
                await _poll_until_socket_recovers(
                    reader, stream, on_fixes, policy, ticker
                )
                failures = 0
                continue
            await ticker.sleep(policy.backoff_seconds(failures))


async def _consume_socket(
    stream: ClairvoyantStream,
    on_fixes: Callable[[list[GpsFix]], None],
    policy: ClairvoyantFailover,
    clock: _Clock,
    *,
    require_healthy: bool,
) -> None:
    subscription = await stream.connect()
    opened = clock.monotonic()
    try:
        while True:
            fix = await subscription.receive()
            if fix is not None:
                on_fixes([fix])
            if (
                require_healthy
                and clock.monotonic() - opened >= policy.healthy_window_seconds
            ):
                return
    finally:
        await subscription.close()


async def _poll_forever(
    reader: ClairvoyantPoller,
    on_fixes: Callable[[list[GpsFix]], None],
    policy: ClairvoyantFailover,
    clock: _Clock,
) -> None:
    while True:
        try:
            on_fixes(await reader.poll())
        except ClairvoyantConnectionError as exc:
            logger.warning("Clairvoyant API poll failed: %s", exc)
        await clock.sleep(policy.probe_interval_seconds)


async def _poll_until_socket_recovers(
    reader: ClairvoyantPoller,
    stream: ClairvoyantStream,
    on_fixes: Callable[[list[GpsFix]], None],
    policy: ClairvoyantFailover,
    clock: _Clock,
) -> None:
    while True:
        try:
            on_fixes(await reader.poll())
        except ClairvoyantConnectionError as exc:
            logger.warning("Clairvoyant API poll failed: %s", exc)
        await clock.sleep(policy.probe_interval_seconds)
        try:
            await _consume_socket(
                stream, on_fixes, policy, clock, require_healthy=True
            )
        except ClairvoyantConnectionError as exc:
            logger.warning("Clairvoyant websocket probe failed: %s", exc)
            continue
        logger.info(
            "Ingestion mode switch: poll -> websocket; "
            "Clairvoyant socket stayed healthy for %ss",
            policy.healthy_window_seconds,
        )
        return
