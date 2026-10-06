import os
from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass(frozen=True)
class Settings:
    environment: str
    navixy_base_url: str
    navixy_api_key: str
    navixy_ws_url: str
    tracker_ids: list[int]
    source_mode: str
    poll_interval_seconds: int

    chatsurfer_base_url: str
    chatsurfer_api_key: str
    chatsurfer_domain_id: str
    chatsurfer_nickname: str
    chatsurfer_classification: str
    chatsurfer_user_high_classification: str
    chatsurfer_rate_limit: float
    chatsurfer_max_message_size: int
    chatsurfer_encryption_required: bool

    public_key_path: str
    private_key_path: str


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
        source_mode=_env(
            "TRACKER_SOURCE_MODE",
            navixy.get("source_mode", "poll"),
        ),
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
        public_key_path=_env(
            "PUBLIC_KEY_PATH",
            crypto.get("public_key_path", "/run/secrets/public_key.pem"),
        ),
        private_key_path=_env(
            "PRIVATE_KEY_PATH",
            crypto.get("private_key_path", "/run/secrets/private_key.pem"),
        ),
    )
