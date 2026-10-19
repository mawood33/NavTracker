import asyncio
import logging

from app.composition import build_application
from app.infrastructure.config import load_settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)


async def main() -> None:
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
