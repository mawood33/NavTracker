"""ChatSurfer, Clairvoyant, crypto, frontend, and reference columns."""

import os

from analytics.application.failover import ClairvoyantFailover
from analytics.domain.columns import ColumnMap
from analytics.infrastructure.config_values import (
    _bool_setting,
    _choice,
    _nested,
    _non_negative,
    _positive,
    _string_list,
)
from navtracker_common.chatsurfer import PAGE_PATH, ChatReadSpec
from navtracker_common.crypto import fernet_key_for_mode
from navtracker_common.settings import env_setting, int_setting, pick_setting


def _rooms(chatsurfer: dict[str, object]) -> tuple[str, ...]:
    raw = pick_setting(
        "CHAT_SURFER_READ_ROOMS",
        chatsurfer.get("read_rooms"),
        "",
    )
    if isinstance(raw, list):
        return tuple(str(item).strip() for item in raw if str(item).strip())
    return tuple(part.strip() for part in str(raw).split(",") if part.strip())

def _failover(clairvoyant: dict[str, object]) -> ClairvoyantFailover:
    return ClairvoyantFailover(
        max_attempts=int(
            _positive(
                "CLAIRVOYANT_FAILOVER_MAX_ATTEMPTS",
                pick_setting(
                    "CLAIRVOYANT_FAILOVER_MAX_ATTEMPTS",
                    clairvoyant.get("failover_max_attempts"),
                    5,
                ),
            )
        ),
        backoff_initial_seconds=_positive(
            "CLAIRVOYANT_FAILOVER_BACKOFF_INITIAL_SECONDS",
            pick_setting(
                "CLAIRVOYANT_FAILOVER_BACKOFF_INITIAL_SECONDS",
                clairvoyant.get("failover_backoff_initial_seconds"),
                1,
            ),
        ),
        backoff_cap_seconds=_positive(
            "CLAIRVOYANT_FAILOVER_BACKOFF_CAP_SECONDS",
            pick_setting(
                "CLAIRVOYANT_FAILOVER_BACKOFF_CAP_SECONDS",
                clairvoyant.get("failover_backoff_cap_seconds"),
                30,
            ),
        ),
        probe_interval_seconds=_positive(
            "CLAIRVOYANT_FAILOVER_PROBE_INTERVAL_SECONDS",
            pick_setting(
                "CLAIRVOYANT_FAILOVER_PROBE_INTERVAL_SECONDS",
                clairvoyant.get("failover_probe_interval_seconds"),
                60,
            ),
        ),
        healthy_window_seconds=_positive(
            "CLAIRVOYANT_FAILOVER_HEALTHY_WINDOW_SECONDS",
            pick_setting(
                "CLAIRVOYANT_FAILOVER_HEALTHY_WINDOW_SECONDS",
                clairvoyant.get("failover_healthy_window_seconds"),
                30,
            ),
        ),
    )

def _fernet_key(crypto: dict[str, object], mode: str) -> bytes | None:
    if mode != "fernet":
        return None
    if "FERNET_KEY" in os.environ:
        configured: bytes | str | None = os.environ["FERNET_KEY"]
    else:
        raw = crypto.get("fernet_key")
        if raw is None or isinstance(raw, (bytes, str)):
            configured = raw
        else:
            configured = str(raw)
    return fernet_key_for_mode(mode, configured)

def _columns(reference: dict[str, object]) -> ColumnMap:
    raw = reference.get("columns") or {}
    if not isinstance(raw, dict):
        raise RuntimeError("reference.columns must be a mapping.")
    columns = ColumnMap()
    aliases = {
        "name": "name",
        "type": "site_type",
        "site_type": "site_type",
        "latitude_dms": "latitude_dms",
        "longitude_dms": "longitude_dms",
        "latitude": "latitude",
        "longitude": "longitude",
        "mgrs": "mgrs",
    }
    for key, field in aliases.items():
        if key in raw and raw[key] is not None:
            columns = columns.override(field, str(raw[key]))
    return columns

def _frontend_block(loaded: dict[str, object]) -> dict[str, object]:
    return _nested(loaded.get("frontend"), "frontend")

def _chat_read_enabled(
    environment: str,
    chatsurfer: dict[str, object],
    rooms_configured: bool,
    path_explicit: bool,
) -> bool:
    if "CHAT_SURFER_READ_ENABLED" in os.environ and os.environ["CHAT_SURFER_READ_ENABLED"].strip():
        return _bool_setting(
            "CHAT_SURFER_READ_ENABLED",
            os.environ["CHAT_SURFER_READ_ENABLED"],
        )
    if "read_enabled" in chatsurfer and chatsurfer["read_enabled"] is not None:
        return _bool_setting("CHAT_SURFER_READ_ENABLED", chatsurfer["read_enabled"])
    if environment == "development":
        return rooms_configured and path_explicit
    return rooms_configured


def _explicit_text(env_name: str, chatsurfer: dict[str, object], key: str) -> str:
    env_value = os.environ.get(env_name)
    if env_value is not None and env_value.strip():
        return env_value.strip()
    raw = chatsurfer.get(key)
    if raw is not None and str(raw).strip():
        return str(raw).strip()
    return ""


def source_settings(
    analytics: dict[str, object],
    chatsurfer: dict[str, object],
    crypto: dict[str, object],
    clairvoyant: dict[str, object],
    reference: dict[str, object],
    loaded: dict[str, object],
    *,
    mode: str,
    encryption_cert: str,
    clairvoyant_mode: str,
    enabled: bool,
    ws_url: str,
    base_url: str,
    speed_unit: str,
    environment: str,
) -> dict[str, object]:
    rooms = _rooms(chatsurfer)
    tacrep = _string_list("CHAT_SURFER_TACREP_ROOMS", chatsurfer.get("tacrep_rooms"))
    explicit_path = _explicit_text("CHAT_SURFER_READ_PATH", chatsurfer, "read_path")
    read_path = explicit_path or "/api/chat/messages/{domainId}/{roomName}"
    page_path = (
        _explicit_text("CHAT_SURFER_READ_PAGE_PATH", chatsurfer, "read_page_path") or PAGE_PATH
    )
    page_size = int(
        _positive(
            "CHAT_SURFER_READ_PAGE_SIZE",
            pick_setting("CHAT_SURFER_READ_PAGE_SIZE", chatsurfer.get("read_page_size"), 100),
        )
    )
    if page_size > 1000:
        page_size = 1000
    return {
        "reference_dir": env_setting(
                    "ANALYTICS_REFERENCE_DIR",
                    str(analytics.get("reference_dir", "")),
                ),
        "chat_poll_seconds": _positive(
                    "ANALYTICS_CHAT_POLL_SECONDS",
                    pick_setting(
                        "ANALYTICS_CHAT_POLL_SECONDS",
                        analytics.get("chat_poll_seconds"),
                        15,
                    ),
                ),
        "chat_speed_unit": speed_unit,
        "read_rooms": rooms,
        "chat_read_enabled": _chat_read_enabled(
            environment,
            chatsurfer,
            bool(rooms or tacrep),
            bool(explicit_path),
        ),
        "chat_read": ChatReadSpec(
            path=read_path,
            page_path=page_path,
            num_after=page_size,
        ),
        "chat_ingest_mode": _choice(
            "CHAT_SURFER_INGEST_MODE",
            pick_setting(
                "CHAT_SURFER_INGEST_MODE",
                chatsurfer.get("ingest_mode"),
                "websocket",
            ),
            {"websocket", "poll"},
        ),
        "chat_backfill_minutes": _non_negative(
            "CHAT_SURFER_BACKFILL_MINUTES",
            pick_setting(
                "CHAT_SURFER_BACKFILL_MINUTES",
                chatsurfer.get("backfill_minutes"),
                60,
            ),
        ),
        "chatsurfer_base_url": env_setting(
                    "CHAT_SURFER_BASE_URL",
                    str(chatsurfer.get("base_url", "")),
                ),
        "chatsurfer_api_key": env_setting("CHAT_SURFER_API_KEY"),
        "chatsurfer_domain_id": env_setting(
                    "CHAT_SURFER_DOMAIN_ID",
                    str(chatsurfer.get("domain_id", "")),
                ),
        "chatsurfer_room_name": env_setting(
                    "CHAT_SURFER_ROOM_NAME",
                    str(chatsurfer.get("room_name", "")),
                ),
        "chatsurfer_room_name_encrypted": env_setting(
                    "CHAT_SURFER_ROOM_NAME_ENCRYPTED",
                    str(chatsurfer.get("room_name_encrypted", "")),
                ),
        "chatsurfer_nickname": env_setting(
                    "CHAT_SURFER_NICKNAME",
                    str(chatsurfer.get("nickname", "navtracker-analytics")),
                ),
        "chatsurfer_classification": env_setting(
                    "CHAT_SURFER_CLASSIFICATION",
                    str(chatsurfer.get("classification", "U")),
                ),
        "chatsurfer_user_high_classification": env_setting(
                    "CHAT_SURFER_USER_HIGH_CLASSIFICATION",
                    str(chatsurfer.get("user_high_classification", "UNCLASSIFIED//FOUO")),
                ),
        "chatsurfer_client_cert": env_setting(
                    "CHAT_SURFER_CLIENT_CERT",
                    str(chatsurfer.get("client_cert", "")),
                ),
        "chatsurfer_client_key": env_setting(
                    "CHAT_SURFER_CLIENT_KEY",
                    str(chatsurfer.get("client_key", "")),
                ),
        "chatsurfer_rate_limit": _positive(
                    "CHAT_SURFER_RATE_LIMIT",
                    pick_setting(
                        "CHAT_SURFER_RATE_LIMIT",
                        chatsurfer.get("rate_limit"),
                        60,
                    ),
                ),
        "chatsurfer_max_message_size": int_setting(
                    "CHAT_SURFER_MAX_MESSAGE_SIZE",
                    env_setting(
                        "CHAT_SURFER_MAX_MESSAGE_SIZE",
                        str(chatsurfer.get("max_message_size", 4000)),
                    ),
                ),
        "chatsurfer_encryption_required": _bool_setting(
                    "CHAT_SURFER_ENCRYPTION_REQUIRED",
                    pick_setting(
                        "CHAT_SURFER_ENCRYPTION_REQUIRED",
                        chatsurfer.get("encryption_required"),
                        False,
                    ),
                ),
        "encryption_cert": encryption_cert,
        "encryption_key": env_setting(
                    "ENCRYPTION_KEY",
                    str(crypto.get("encryption_key", "")),
                ),
        "mode": mode,
        "fernet_key": _fernet_key(crypto, mode),
        "clairvoyant_enabled": enabled,
        "clairvoyant_mode": clairvoyant_mode,
        "clairvoyant_ws_url": ws_url,
        "clairvoyant_base_url": base_url,
        "clairvoyant_api_key": env_setting(
                    "CLAIRVOYANT_API_KEY",
                    str(clairvoyant.get("api_key", "")),
                ),
        "clairvoyant_layer": env_setting(
                    "CLAIRVOYANT_LAYER",
                    str(clairvoyant.get("layer", "default")),
                ),
        "clairvoyant_failover": _failover(clairvoyant),
        "tacrep_rooms": tacrep,
        "chat_page_size": page_size,
        "chat_exercise_only": _bool_setting(
            "CHAT_SURFER_EXERCISE_ONLY",
            pick_setting(
                "CHAT_SURFER_EXERCISE_ONLY",
                chatsurfer.get("exercise_only"),
                False,
            ),
        ),
        "chat_skip_bots": _string_list(
            "CHAT_SURFER_SKIP_BOTS",
            chatsurfer.get("skip_bots"),
        ),
        "chat_seen_limit": int(
            _positive(
                "CHAT_SURFER_SEEN_LIMIT",
                pick_setting("CHAT_SURFER_SEEN_LIMIT", chatsurfer.get("seen_limit"), 2000),
            )
        ),
        "chat_scrub_poll_seconds": _positive(
            "CHAT_SURFER_SCRUB_POLL_SECONDS",
            pick_setting(
                "CHAT_SURFER_SCRUB_POLL_SECONDS",
                chatsurfer.get("scrub_poll_seconds"),
                60,
            ),
        ),
        "chat_failover_max_attempts": int(
            _positive(
                "CHAT_SURFER_FAILOVER_MAX_ATTEMPTS",
                pick_setting(
                    "CHAT_SURFER_FAILOVER_MAX_ATTEMPTS",
                    chatsurfer.get("failover_max_attempts"),
                    5,
                ),
            )
        ),
        "chat_failover_backoff_initial_seconds": _positive(
            "CHAT_SURFER_FAILOVER_BACKOFF_INITIAL_SECONDS",
            pick_setting(
                "CHAT_SURFER_FAILOVER_BACKOFF_INITIAL_SECONDS",
                chatsurfer.get("failover_backoff_initial_seconds"),
                1,
            ),
        ),
        "chat_failover_backoff_cap_seconds": _positive(
            "CHAT_SURFER_FAILOVER_BACKOFF_CAP_SECONDS",
            pick_setting(
                "CHAT_SURFER_FAILOVER_BACKOFF_CAP_SECONDS",
                chatsurfer.get("failover_backoff_cap_seconds"),
                30,
            ),
        ),
        "chat_failover_probe_interval_seconds": _positive(
            "CHAT_SURFER_FAILOVER_PROBE_INTERVAL_SECONDS",
            pick_setting(
                "CHAT_SURFER_FAILOVER_PROBE_INTERVAL_SECONDS",
                chatsurfer.get("failover_probe_interval_seconds"),
                60,
            ),
        ),
        "clairvoyant_report_layers": _string_list(
                    "CLAIRVOYANT_REPORT_LAYERS",
                    clairvoyant.get("report_layers"),
                ),
        "frontend_enabled": _bool_setting(
                    "ANALYTICS_FRONTEND_ENABLED",
                    pick_setting(
                        "ANALYTICS_FRONTEND_ENABLED",
                        _frontend_block(loaded).get("enabled"),
                        False,
                    ),
                ),
        "frontend_ingest_url": env_setting(
                    "ANALYTICS_FRONTEND_INGEST_URL",
                    str(_frontend_block(loaded).get("ingest_url", "")),
                ).strip(),
        "columns": _columns(reference),
    }
