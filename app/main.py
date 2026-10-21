import asyncio
import logging

from app.composition import build_application
from app.infrastructure.config import load_settings
from navtracker_common.logging_setup import configure_logging

DEFAULT_LOG_FILE = "/app/logs/navtracker.log"


async def main() -> None:
    configure_logging(DEFAULT_LOG_FILE)
    settings = load_settings()
    app = build_application(settings)
    try:
        await app.supervisor.run()
    except Exception as e:
        logging.exception(f"Error starting app: {e}")
    finally:
        await app.aclose()


if __name__ == "__main__":
    asyncio.run(main())
