import asyncio
from dataclasses import dataclass

from analytics.application.analysis import AnalysisService
from analytics.application.catalog import CatalogService
from analytics.application.dispatch import ingest_room_list
from analytics.application.failover import run_clairvoyant
from analytics.application.hub import OutputHub
from analytics.application.ingest import IngestService
from analytics.application.loop import AnalysisLoop
from analytics.application.ports import RoomPublisher
from analytics.application.publishing import PublishService
from analytics.application.query import QueryService
from analytics.application.routing import RoutingService
from analytics.application.service import AnalyticsService, DocumentSink, ReportSource
from analytics.application.state import make_state
from analytics.domain.routing import RouteCache
from analytics.infrastructure.chat_ws import ChatStompStream
from analytics.infrastructure.clairvoyant import (
    ClairvoyantApiClient,
    ClairvoyantReportClient,
    ClairvoyantWebSocket,
)
from analytics.infrastructure.config import AnalyticsSettings
from analytics.infrastructure.frontend import FrontendPublisher
from analytics.infrastructure.h3_index import H3Index
from analytics.infrastructure.mgrs_grid import MgrsLibrary
from analytics.infrastructure.reference_parse import FileReferences
from analytics.infrastructure.room_publish import ChatRoomPublisher
from analytics.infrastructure.routing_engines import build_router
from navtracker_common.chatsurfer import ChatSurferApiClient, select_chat_rooms
from navtracker_common.crypto import CryptoHandler


@dataclass
class AnalyticsApplication:
    """Objects created at startup. Nothing here has opened a connection."""

    settings: AnalyticsSettings
    crypto_handler: CryptoHandler | None
    chat: ChatSurferApiClient | None
    service: AnalyticsService
    clairvoyant_reader: ClairvoyantApiClient | None
    clairvoyant_stream: ClairvoyantWebSocket | None
    report_client: ClairvoyantReportClient | None
    frontend: FrontendPublisher | None

    async def aclose(self) -> None:
        if self.chat is not None:
            await self.chat.close()
        if self.clairvoyant_reader is not None:
            await self.clairvoyant_reader.close()
        if self.report_client is not None:
            await self.report_client.close()
        if self.frontend is not None:
            await self.frontend.close()


def wire_service(
    settings: AnalyticsSettings,
    chat: ChatSurferApiClient | None = None,
    report_source: ReportSource | None = None,
    frontend: DocumentSink | None = None,
    publisher: RoomPublisher | None = None,
) -> AnalyticsService:
    """Composition root. Application code does not import these adapters."""

    if publisher is None and chat is not None:
        publisher = ChatRoomPublisher(chat)
    state = make_state(
        settings,
        chat,
        report_source,
        frontend,
        index=H3Index(settings.grid.resolution),
        router=build_router(
            settings.routing_engine,
            settings.routing_base_url,
            RouteCache(settings.routing_cache_size, settings.routing_cache_ttl_seconds),
            settings.routing_snap_meters,
            settings.routing_join_meters,
            settings.routing_mapbox_token,
            settings.routing_timeout_seconds,
        ),
        references=FileReferences(),
        grid=MgrsLibrary(),
        hub=OutputHub(),
        publisher=publisher,
    )
    query = QueryService(state)
    ingest = IngestService(state)
    routing = RoutingService(state)
    catalog = CatalogService(state)
    publish = PublishService(state, query)
    analysis = AnalysisService(state)
    loop = AnalysisLoop(state, ingest, analysis, publish)
    catalog.open()
    service = AnalyticsService(
        state,
        query=query,
        ingest=ingest,
        routing=routing,
        catalog=catalog,
        publish=publish,
        analysis=analysis,
        loop=loop,
    )
    if (
        chat is not None
        and settings.chat_read_enabled
        and settings.chat_ingest_mode == "websocket"
    ):
        rooms = select_chat_rooms(
            tuple(
                dict.fromkeys(
                    (*settings.read_rooms, *ingest_room_list(state._coord_card, settings))
                )
            ),
            encryption_required=settings.chatsurfer_encryption_required,
            room_name_encrypted=settings.chatsurfer_room_name_encrypted,
        )
        if rooms:
            service._loop._stream = ChatStompStream(
                chat,
                rooms,
                api_key=settings.chatsurfer_api_key,
                client_cert=settings.chatsurfer_client_cert,
                client_key=settings.chatsurfer_client_key,
            )
    return service


def build_analytics(settings: AnalyticsSettings) -> AnalyticsApplication:
    """Construct clients and the analyzer. Does not bind, connect, or publish."""

    crypto_handler = _crypto(settings)
    needs_chat = (
        bool(settings.read_rooms)
        or settings.publish_events
        or bool(settings.tacrep_rooms)
        or bool(settings.default_ingest_rooms)
        or bool(settings.default_publish_rooms)
        or bool(settings.room_ingest)
        or bool(settings.room_publish)
        or bool(settings.publish_rules_text)
    )
    chat: ChatSurferApiClient | None = None
    if needs_chat:
        chat = ChatSurferApiClient(
            base_url=settings.chatsurfer_base_url,
            api_key=settings.chatsurfer_api_key,
            domain_id=settings.chatsurfer_domain_id,
            room_name=settings.chatsurfer_room_name,
            room_name_encrypted=settings.chatsurfer_room_name_encrypted,
            nickname=settings.chatsurfer_nickname,
            classification=settings.chatsurfer_classification,
            user_high_classification=settings.chatsurfer_user_high_classification,
            rate_limit=settings.chatsurfer_rate_limit,
            max_message_size=settings.chatsurfer_max_message_size,
            encryption_required=settings.chatsurfer_encryption_required,
            crypto_handler=crypto_handler,
            client_cert=settings.chatsurfer_client_cert or None,
            client_key=settings.chatsurfer_client_key or None,
            read_spec=settings.chat_read,
        )
    reader: ClairvoyantApiClient | None = None
    stream: ClairvoyantWebSocket | None = None
    if settings.clairvoyant_enabled:
        reader = ClairvoyantApiClient(
            base_url=settings.clairvoyant_base_url,
            api_key=settings.clairvoyant_api_key,
            layer=settings.clairvoyant_layer,
        )
        stream = ClairvoyantWebSocket(
            ws_url=settings.clairvoyant_ws_url,
            api_key=settings.clairvoyant_api_key,
            layer=settings.clairvoyant_layer,
        )
    report_client: ClairvoyantReportClient | None = None
    if settings.clairvoyant_enabled and settings.clairvoyant_report_layers:
        report_client = ClairvoyantReportClient(
            base_url=settings.clairvoyant_base_url,
            api_key=settings.clairvoyant_api_key,
            layers=settings.clairvoyant_report_layers,
            policy=settings.report_policy,
        )
    frontend: FrontendPublisher | None = None
    if settings.frontend_enabled:
        frontend = FrontendPublisher(settings.frontend_ingest_url)
    return AnalyticsApplication(
        settings=settings,
        crypto_handler=crypto_handler,
        chat=chat,
        service=wire_service(settings, chat, report_client, frontend),
        clairvoyant_reader=reader,
        clairvoyant_stream=stream,
        report_client=report_client,
        frontend=frontend,
    )


async def run_sources(app: AnalyticsApplication) -> None:
    """Chat polling, optional Clairvoyant failover, and the analysis loop."""

    tasks = [
        asyncio.create_task(app.service.run_chat_loop(), name="analytics-chat"),
        asyncio.create_task(
            app.service.run_analysis_loop(),
            name="analytics-analyze",
        ),
    ]
    reader = app.clairvoyant_reader
    stream = app.clairvoyant_stream
    if app.settings.clairvoyant_enabled and reader is not None and stream is not None:
        tasks.append(
            asyncio.create_task(
                run_clairvoyant(
                    mode=app.settings.clairvoyant_mode,
                    reader=reader,
                    stream=stream,
                    policy=app.settings.clairvoyant_failover,
                    on_fixes=app.service.add_fixes,
                ),
                name="analytics-clairvoyant",
            )
        )
    if app.report_client is not None:
        tasks.append(
            asyncio.create_task(
                app.service.run_report_loop(),
                name="analytics-reports",
            )
        )
    try:
        await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def _crypto(settings: AnalyticsSettings) -> CryptoHandler | None:
    if not settings.chatsurfer_encryption_required:
        return None
    if settings.mode == "fernet":
        return CryptoHandler(mode="fernet", fernet_key=settings.fernet_key)
    private = settings.encryption_key.strip() or None
    return CryptoHandler(
        mode=settings.mode,
        public_key_path=settings.encryption_cert,
        private_key_path=private,
    )
