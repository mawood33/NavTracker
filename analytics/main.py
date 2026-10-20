import asyncio
import logging

from analytics.application.dispatch import truth_read_rooms
from analytics.composition import build_analytics, run_sources
from analytics.infrastructure.config import load_analytics_settings
from analytics.infrastructure.http_server import serve

logger = logging.getLogger(__name__)


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    settings = load_analytics_settings()
    app = build_analytics(settings)
    app.service.load_reference()
    logger.info(
        "Analytics startup env=%s clairvoyant=%s mode=%s rooms=%s",
        settings.environment,
        settings.clairvoyant_enabled,
        settings.clairvoyant_mode,
        list(truth_read_rooms(settings)),
    )

    async def _run() -> None:
        server = asyncio.create_task(
            serve(settings, app.service),
            name="analytics-http",
        )
        sources = asyncio.create_task(run_sources(app), name="analytics-sources")
        try:
            await asyncio.gather(server, sources)
        finally:
            server.cancel()
            sources.cancel()
            await asyncio.gather(server, sources, return_exceptions=True)
            await app.aclose()

    asyncio.run(_run())


if __name__ == "__main__":
    main()
