"""HTTP bind address, tokens, websocket limits, and body caps."""

import os

from analytics.infrastructure.config_values import (
    _byte_setting,
    _non_negative,
    _positive,
)
from navtracker_common.settings import env_setting, int_setting, pick_setting


def _count_setting(name: str, configured: object, default: int) -> int:
    number = _non_negative(name, pick_setting(name, configured, default))
    if number != int(number):
        raise RuntimeError(f"Invalid {name}={configured!r}. Expected an integer.")
    return int(number)


def _ws_origins(analytics: dict[str, object]) -> tuple[str, ...]:
    raw = os.getenv("ANALYTICS_WS_ORIGINS", "").strip()
    if raw:
        return tuple(part.strip() for part in raw.split(",") if part.strip())
    configured = analytics.get("ws_origins", ())
    if isinstance(configured, str):
        return tuple(part.strip() for part in configured.split(",") if part.strip())
    if isinstance(configured, list):
        return tuple(str(part).strip() for part in configured if str(part).strip())
    return ()

def http_settings(analytics: dict[str, object]) -> dict[str, object]:
    return {
        "http_host": env_setting(
                    "ANALYTICS_HTTP_HOST",
                    str(analytics.get("http_host", "127.0.0.1")),
                ),
        "http_port": int_setting(
                    "ANALYTICS_HTTP_PORT",
                    env_setting(
                        "ANALYTICS_HTTP_PORT",
                        str(analytics.get("http_port", 8080)),
                    ),
                ),
        "read_token": env_setting("ANALYTICS_READ_TOKEN", str(analytics.get("read_token", ""))),
        "write_token": env_setting("ANALYTICS_WRITE_TOKEN", str(analytics.get("write_token", ""))),
        "ws_origins": _ws_origins(analytics),
        "max_body_bytes": _byte_setting(
                    "ANALYTICS_MAX_BODY_BYTES",
                    analytics.get("max_body_bytes"),
                    1_048_576,
                ),
        "max_upload_bytes": _byte_setting(
                    "ANALYTICS_MAX_UPLOAD_BYTES",
                    analytics.get("max_upload_bytes"),
                    8_388_608,
                ),
        "max_ws_bytes": _byte_setting(
                    "ANALYTICS_MAX_WS_BYTES",
                    analytics.get("max_ws_bytes"),
                    1_048_576,
                ),
        "max_archive_entries": int(
                    _byte_setting(
                        "ANALYTICS_MAX_ARCHIVE_ENTRIES",
                        analytics.get("max_archive_entries"),
                        64,
                    )
                ),
        "max_archive_bytes": _byte_setting(
                    "ANALYTICS_MAX_ARCHIVE_BYTES",
                    analytics.get("max_archive_bytes"),
                    33_554_432,
                ),
        "max_ws_queue": int(
                    _byte_setting("ANALYTICS_MAX_WS_QUEUE", analytics.get("max_ws_queue"), 8)
                ),
        "max_fixes_per_vehicle": _count_setting(
            "ANALYTICS_MAX_FIXES_PER_VEHICLE",
            analytics.get("max_fixes_per_vehicle"),
            100,
        ),
        "fix_retention_seconds": _non_negative(
                    "ANALYTICS_FIX_RETENTION_SECONDS",
                    pick_setting(
                        "ANALYTICS_FIX_RETENTION_SECONDS",
                        analytics.get("fix_retention_seconds"),
                        86_400,
                    ),
                ),
        "public_base_url": env_setting(
                    "ANALYTICS_PUBLIC_BASE_URL",
                    str(analytics.get("public_base_url", "")),
                ).strip(),
        "analysis_interval_seconds": _positive(
                    "ANALYTICS_INTERVAL_SECONDS",
                    pick_setting(
                        "ANALYTICS_INTERVAL_SECONDS",
                        analytics.get("analysis_interval_seconds"),
                        30,
                    ),
                ),
    }
