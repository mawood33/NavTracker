"""Websocket-to-REST failover for ChatSurfer ingest.

Mirrors Clairvoyant: exponential backoff, then REST polling while a probe
tries the socket. ``poll`` mode never opens the websocket. Only
``ChatConnectionError`` counts as a socket failure.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from analytics.application.ports import ChatConnectionError, ChatEventStream

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ChatFailover:
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


async def run_chat_ingest(
    *,
    mode: str,
    stream: ChatEventStream | None,
    poll: Callable[[], Awaitable[None]],
    on_message: Callable[[object], None],
    on_scrub: Callable[[str], None],
    policy: ChatFailover,
    poll_interval: float = 15.0,
    clock: _Clock | None = None,
    on_mode: Callable[[str], None] | None = None,
) -> None:
    """Read until cancelled. ``mode`` is ``websocket`` or ``poll``."""

    ticker = clock or _Clock()
    if mode == "poll" or stream is None:
        if on_mode is not None:
            on_mode("poll")
        await _poll_forever(poll, ticker, poll_interval)
        return
    if on_mode is not None:
        on_mode("websocket")
    failures = 0
    while True:
        try:
            await _consume(stream, on_message, on_scrub, policy, ticker, require_healthy=False)
            failures = 0
        except ChatConnectionError as exc:
            failures += 1
            logger.warning("ChatSurfer websocket failure attempt=%s", failures)
            logger.debug("ChatSurfer websocket failure detail: %s", exc)
            if failures >= policy.max_attempts:
                logger.info(
                    "Ingestion mode switch: websocket -> poll after %s "
                    "consecutive ChatSurfer socket failures",
                    failures,
                )
                if on_mode is not None:
                    on_mode("poll")
                await _poll_until_socket_recovers(
                    stream, poll, on_message, on_scrub, policy, ticker
                )
                failures = 0
                if on_mode is not None:
                    on_mode("websocket")
                continue
            await ticker.sleep(policy.backoff_seconds(failures))


async def _consume(
    stream: ChatEventStream,
    on_message: Callable[[object], None],
    on_scrub: Callable[[str], None],
    policy: ChatFailover,
    clock: _Clock,
    *,
    require_healthy: bool,
) -> None:
    subscription = await stream.connect()
    opened = clock.monotonic()
    try:
        while True:
            event = await subscription.receive()
            if event is not None:
                kind, payload = event
                if kind == "message":
                    on_message(payload)
                elif kind == "scrub" and isinstance(payload, str):
                    on_scrub(payload)
            if require_healthy and clock.monotonic() - opened >= policy.healthy_window_seconds:
                return
    finally:
        await subscription.close()


async def _poll_forever(
    poll: Callable[[], Awaitable[None]],
    clock: _Clock,
    interval: float,
) -> None:
    while True:
        try:
            await poll()
        except ChatConnectionError:
            logger.warning("ChatSurfer REST poll failed")
        except Exception:
            logger.exception("ChatSurfer REST poll failed")
        await clock.sleep(interval)


async def _poll_until_socket_recovers(
    stream: ChatEventStream,
    poll: Callable[[], Awaitable[None]],
    on_message: Callable[[object], None],
    on_scrub: Callable[[str], None],
    policy: ChatFailover,
    clock: _Clock,
) -> None:
    while True:
        try:
            await poll()
        except ChatConnectionError:
            logger.warning("ChatSurfer REST poll failed")
        except Exception:
            logger.exception("ChatSurfer REST poll failed")
        await clock.sleep(policy.probe_interval_seconds)
        try:
            await _consume(
                stream,
                on_message,
                on_scrub,
                policy,
                clock,
                require_healthy=True,
            )
        except ChatConnectionError:
            logger.warning("ChatSurfer websocket probe failed")
            continue
        logger.info(
            "Ingestion mode switch: poll -> websocket; "
            "ChatSurfer socket stayed healthy for %ss",
            policy.healthy_window_seconds,
        )
        return
