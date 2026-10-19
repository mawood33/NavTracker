import os
from dataclasses import dataclass
from pathlib import Path

import yaml

from app.application.mode import (
    DEFAULT_BACKOFF_CAP_SECONDS,
    DEFAULT_BACKOFF_INITIAL_SECONDS,
    DEFAULT_HEALTHY_WINDOW_SECONDS,
    DEFAULT_MAX_ATTEMPTS,
    DEFAULT_PROBE_INTERVAL_SECONDS,
    FailoverPolicy,
    IngestionMode,
    build_failover_policy,
    resolve_navixy_mode,
)


@dataclass(frozen=True)
class Settings:
    environment: str
    navixy_base_url: str
    navixy_api_key: str
    navixy_ws_url: str
    tracker_ids: list[int]
    navixy_mode: IngestionMode
    poll_interval_seconds: int
    failover: FailoverPolicy

    chatsurfer_base_url: str
    chatsurfer_api_key: str
    chatsurfer_domain_id: str
    chatsurfer_room_name: str
    chatsurfer_room_name_encrypted: str
    chatsurfer_nickname: str
    chatsurfer_classification: str
    chatsurfer_user_high_classification: str
    chatsurfer_client_cert: str
    chatsurfer_client_key: str
    chatsurfer_rate_limit: float
    chatsurfer_max_message_size: int
    chatsurfer_encryption_required: bool

    encryption_cert: str
    encryption_key: str
    mode: str
    fernet_key: bytes

def _env(name: str, default: str | None = None) -> str:
    value = os.getenv(name, default)
    if value is None:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def load_settings() -> Settings:
    environment = os.getenv("APP_ENV", "development")
    config_path = Path(f"config/{environment}.yaml")
    with config_path.open("r", encoding="utf-8") as file:
        config = yaml.safe_load(file) or {}

    navixy = config.get("navixy", {})
    chatsurfer = config.get("chatsurfer", {})
    crypto = config.get("crypto", {})
    
    tracker_ids = [
        int(item.strip())
        for item in _env(
            "TRACKER_IDS",
            ",".join(map(str, navixy.get("tracker_ids", []))),
        ).split(",")
        if item.strip()
    ]
    _reject_legacy_source_mode(navixy)
    navixy_mode = resolve_navixy_mode(
        os.getenv("NAVIXY_MODE"),
        navixy.get("mode"),
    )
    failover = build_failover_policy(
        max_attempts=_pick(
            "NAVIXY_FAILOVER_MAX_ATTEMPTS",
            navixy.get("failover_max_attempts"),
            DEFAULT_MAX_ATTEMPTS,
        ),
        backoff_initial_seconds=_pick(
            "NAVIXY_FAILOVER_BACKOFF_INITIAL_SECONDS",
            navixy.get("failover_backoff_initial_seconds"),
            DEFAULT_BACKOFF_INITIAL_SECONDS,
        ),
        backoff_cap_seconds=_pick(
            "NAVIXY_FAILOVER_BACKOFF_CAP_SECONDS",
            navixy.get("failover_backoff_cap_seconds"),
            DEFAULT_BACKOFF_CAP_SECONDS,
        ),
        probe_interval_seconds=_pick(
            "NAVIXY_FAILOVER_PROBE_INTERVAL_SECONDS",
            navixy.get("failover_probe_interval_seconds"),
            DEFAULT_PROBE_INTERVAL_SECONDS,
        ),
        healthy_window_seconds=_pick(
            "NAVIXY_FAILOVER_HEALTHY_WINDOW_SECONDS",
            navixy.get("failover_healthy_window_seconds"),
            DEFAULT_HEALTHY_WINDOW_SECONDS,
        ),
    )

    return Settings(
        environment=environment,
        navixy_base_url=_env(
            "NAVIXY_BASE_URL",
            navixy.get("base_url"),
        ),
        navixy_api_key=_env("NAVIXY_API_KEY"),
        navixy_ws_url=_env(
            "NAVIXY_WS_URL",
            navixy.get("ws_url"),
        ),
        tracker_ids=tracker_ids,
        navixy_mode=navixy_mode,
        poll_interval_seconds=int(
            _env(
                "POLL_INTERVAL_SECONDS",
                str(navixy.get("poll_interval_seconds", 30)),
            )
        ),
        chatsurfer_base_url=_env(
            "CHAT_SURFER_BASE_URL",
            chatsurfer.get("base_url"),
        ),
        chatsurfer_api_key=_env("CHAT_SURFER_API_KEY"),
        chatsurfer_domain_id=_env(
            "CHAT_SURFER_DOMAIN_ID",
            chatsurfer.get("domain_id"),
        ),
        chatsurfer_room_name=_env(
            "CHAT_SURFER_ROOM_NAME",
            chatsurfer.get("room_name"),
        ),
        chatsurfer_room_name_encrypted=_env(
            "CHAT_SURFER_ROOM_NAME_ENCRYPTED",
            chatsurfer.get("room_name_encrypted"),
        ),
        chatsurfer_nickname=_env(
            "CHAT_SURFER_NICKNAME",
            chatsurfer.get("nickname", "navtracker"),
        ),
        chatsurfer_classification=_env(
            "CHAT_SURFER_CLASSIFICATION",
            chatsurfer.get("classification", "U"),
        ),
        chatsurfer_user_high_classification=_env(
            "CHAT_SURFER_USER_HIGH_CLASSIFICATION",
            chatsurfer.get(
                "user_high_classification",
                "UNCLASSIFIED//FOUO",
            ),
        ),
        chatsurfer_client_cert=_env(
            "CHAT_SURFER_CLIENT_CERT",
            chatsurfer.get(
                "client_cert",
                "/data/certs/client/tls.crt",
            ),
        ),
        chatsurfer_client_key=_env(
            "CHAT_SURFER_CLIENT_KEY",
            chatsurfer.get(
                "client_key",
                "/data/certs/client/tls.key",
            ),
        ),
        chatsurfer_rate_limit=float(
            _env(
                "CHAT_SURFER_RATE_LIMIT",
                str(chatsurfer.get("rate_limit", 60)),
            )
        ),
        chatsurfer_max_message_size=int(
            _env(
                "CHAT_SURFER_MAX_MESSAGE_SIZE",
                str(chatsurfer.get("max_message_size", 4000)),
            )
        ),
        chatsurfer_encryption_required=(
            _env(
                "CHAT_SURFER_ENCRYPTION_REQUIRED",
                str(chatsurfer.get("encryption_required", False)),
            ).lower()
            == "true"
        ),
        encryption_cert=_env(
            "ENCRYPTION_CERT",
            crypto.get("encryption_cert", "/data/certs/client/tls.crt"),
        ),
        encryption_key=_env(
            "ENCRYPTION_KEY",
            crypto.get("encryption_key", "/data/certs/client/tls.key"),
        ),
        mode=_env(
            "MODE",
            crypto.get("mode", "pem"),
        ),
        fernet_key=_env(
            "FERNET_KEY",
            crypto.get("fernet_key", None),
        ),
        failover=failover,
    )


def _pick(env_name: str, yaml_value: object, default: object) -> object:
    env_value = os.getenv(env_name)
    if env_value is not None and env_value.strip() != "":
        return env_value.strip()
    if yaml_value is not None:
        return yaml_value
    return default


def _reject_legacy_source_mode(navixy: dict) -> None:
    env_mode = os.getenv("NAVIXY_MODE")
    env_mode_set = env_mode is not None and env_mode.strip() != ""
    if os.getenv("TRACKER_SOURCE_MODE") and not env_mode_set:
        raise RuntimeError(
            "TRACKER_SOURCE_MODE is no longer supported. "
            "Set NAVIXY_MODE to 'websocket' or 'poll'."
        )
    if (
        "source_mode" in navixy
        and "mode" not in navixy
        and not env_mode_set
    ):
        raise RuntimeError(
            "navixy.source_mode is no longer supported. "
            "Set navixy.mode or NAVIXY_MODE to 'websocket' or 'poll'."
        )
