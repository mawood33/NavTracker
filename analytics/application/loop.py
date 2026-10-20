"""Analysis, chat, and report loops. They call the focused services."""

import asyncio
import logging
import time
from collections.abc import Callable

from analytics.application.analysis import AnalysisService
from analytics.application.chat_failover import ChatFailover, run_chat_ingest
from analytics.application.ingest import IngestService
from analytics.application.ports import ChatEventStream
from analytics.application.publishing import PublishService
from analytics.application.state import AnalyticsState
from navtracker_common.chatsurfer import ChatReadError, ChatRoomMessage, select_chat_rooms

logger = logging.getLogger(__name__)


class AnalysisLoop:
    """The orchestrator. It does not own catalog data."""

    def __init__(
        self,
        state: AnalyticsState,
        ingest: IngestService,
        analysis: AnalysisService,
        publish: PublishService,
        stream: ChatEventStream | None = None,
    ) -> None:
        self._state = state
        self._ingest = ingest
        self._analysis = analysis
        self._publish = publish
        self._stream = stream
        self._last_scrub = 0.0
        self._catalog_checked = False

    async def run_chat_loop(self) -> None:
        settings = self._state.settings
        if not settings.chat_read_enabled:
            self._state._chat_transport = "disabled"
            while True:
                await asyncio.sleep(settings.chat_poll_seconds or 15)
        await self._warn_catalog()
        policy = ChatFailover(
            max_attempts=settings.chat_failover_max_attempts,
            backoff_initial_seconds=settings.chat_failover_backoff_initial_seconds,
            backoff_cap_seconds=settings.chat_failover_backoff_cap_seconds,
            probe_interval_seconds=settings.chat_failover_probe_interval_seconds,
        )
        await run_chat_ingest(
            mode=settings.chat_ingest_mode,
            stream=self._stream,
            poll=self._poll_once,
            on_message=self._on_message,
            on_scrub=self._on_scrub,
            policy=policy,
            poll_interval=settings.chat_poll_seconds,
            on_mode=self._set_mode,
        )

    async def _poll_once(self) -> None:
        try:
            self._ingest.add_fixes(await self._ingest.poll_chat())
            self._ingest.add_reports(await self._ingest.poll_tacrep())
            await self._maybe_scrub()
        except asyncio.CancelledError:
            raise
        except ChatReadError as exc:
            code = exc.status_code if exc.status_code is not None else exc.kind
            logger.error("ChatSurfer read failed status=%s path=%s", code, exc.path)
        except Exception:
            logger.exception("ChatSurfer room read failed")

    async def _maybe_scrub(self) -> None:
        interval = self._state.settings.chat_scrub_poll_seconds
        now = time.monotonic()
        if now - self._last_scrub < interval:
            return
        self._last_scrub = now
        await self._ingest.poll_scrubbed()
        await asyncio.to_thread(self._analysis.analyze)

    def _on_message(self, payload: object) -> None:
        if not isinstance(payload, ChatRoomMessage):
            return
        self._ingest.consume_messages(payload.room_name or "chat", [payload])

    def _on_scrub(self, message_id: str) -> None:
        self._ingest.retract(message_id)
        self._analysis.analyze()

    def _set_mode(self, mode: str) -> None:
        self._state._chat_transport = mode

    async def _warn_catalog(self) -> None:
        if self._catalog_checked:
            return
        self._catalog_checked = True
        client = self._state.chat
        if client is None or not hasattr(client, "warn_catalog"):
            return
        from analytics.application.dispatch import ingest_room_list

        rooms = select_chat_rooms(
            tuple(
                dict.fromkeys(
                    (
                        *self._state.settings.read_rooms,
                        *ingest_room_list(self._state._coord_card, self._state.settings),
                    )
                )
            ),
            encryption_required=self._state.settings.chatsurfer_encryption_required,
            room_name_encrypted=self._state.settings.chatsurfer_room_name_encrypted,
        )
        try:
            await client.warn_catalog(rooms)
        except Exception:
            logger.warning("ChatSurfer catalog check failed")

    async def run_report_loop(self) -> None:
        if self._state.report_source is None:
            return
        while True:
            try:
                self._ingest.add_reports(await self._ingest.poll_report_layers())
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Clairvoyant report layer read failed")
            await asyncio.sleep(self._state.settings.chat_poll_seconds)

    async def run_analysis_loop(self, analyze: Callable[[], None] | None = None) -> None:
        step = analyze or self._analysis.analyze
        while True:
            try:
                async with self._state._lock:
                    await asyncio.to_thread(step)
                    await self._publish.publish_if_configured()
                    await self._publish.publish_routed()
                    message = self._publish.publish_output()
                    if message is not None and self._state.frontend is not None:
                        try:
                            await self._state.frontend.publish(message)
                        except Exception:
                            logger.exception("Frontend publication failed")
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Analysis failed")
            await asyncio.sleep(self._state.settings.analysis_interval_seconds)
