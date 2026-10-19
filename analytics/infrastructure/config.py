"""Load analytics settings. Section modules own each group of keys.

Existing environment names and defaults are unchanged. This module only
opens the YAML file, runs the early mode checks, and assembles
``AnalyticsSettings``.
"""

import os
from pathlib import Path

import yaml

from analytics.application.settings import AnalyticsSettings
from analytics.infrastructure.config_coord import coord_settings
from analytics.infrastructure.config_grid import grid_settings
from analytics.infrastructure.config_http import http_settings
from analytics.infrastructure.config_map import map_settings
from analytics.infrastructure.config_output import output_coord_settings
from analytics.infrastructure.config_rooms import room_settings
from analytics.infrastructure.config_sources import source_settings
from analytics.infrastructure.config_values import _bool_setting, _choice, _mapping
from navtracker_common.crypto import require_public_material
from navtracker_common.settings import env_setting, pick_setting

_CRYPTO_MODES = {"pem", "ssh", "fernet"}
_SPEED_UNITS = {"mps", "kmh"}
_CLAIRVOYANT_MODES = {"websocket", "poll"}


def load_analytics_settings(
    config_dir: str | Path = "config/analytics",
) -> AnalyticsSettings:
    environment = os.getenv("APP_ENV", "development")
    config_path = Path(config_dir) / f"{environment}.yaml"
    if not config_path.is_file():
        raise RuntimeError(
            f"APP_ENV='{environment}' has no analytics config at {config_path}. "
            "Expected one of: development, staging, production."
        )
    with config_path.open("r", encoding="utf-8") as file:
        loaded = yaml.safe_load(file) or {}
    if not isinstance(loaded, dict):
        raise RuntimeError(f"{config_path} must be a mapping.")
    analytics = _mapping(loaded.get("analytics"), "analytics")
    chatsurfer = _mapping(loaded.get("chatsurfer"), "chatsurfer")
    crypto = _mapping(loaded.get("crypto"), "crypto")
    clairvoyant = _mapping(loaded.get("clairvoyant"), "clairvoyant")
    reference = _mapping(loaded.get("reference"), "reference")
    mode = _choice(
        "MODE",
        pick_setting("MODE", crypto.get("mode"), "pem"),
        _CRYPTO_MODES,
    )
    encryption_cert = env_setting(
        "ENCRYPTION_CERT",
        str(crypto.get("encryption_cert", "/data/certs/client/tls.crt")),
    ).strip()
    require_public_material(mode, encryption_cert)
    clairvoyant_mode = _choice(
        "CLAIRVOYANT_MODE",
        pick_setting("CLAIRVOYANT_MODE", clairvoyant.get("mode"), "websocket"),
        _CLAIRVOYANT_MODES,
    )
    enabled = _bool_setting(
        "CLAIRVOYANT_ENABLED",
        pick_setting("CLAIRVOYANT_ENABLED", clairvoyant.get("enabled"), False),
    )
    ws_url = env_setting(
        "CLAIRVOYANT_WS_URL",
        str(clairvoyant.get("ws_url", "")),
    ).strip()
    base_url = env_setting(
        "CLAIRVOYANT_BASE_URL",
        str(clairvoyant.get("base_url", "")),
    ).strip()
    if enabled and clairvoyant_mode == "websocket" and not ws_url:
        raise RuntimeError(
            "CLAIRVOYANT_MODE=websocket requires CLAIRVOYANT_WS_URL."
        )
    if enabled and not base_url:
        raise RuntimeError(
            "Clairvoyant failover requires CLAIRVOYANT_BASE_URL."
        )
    speed_unit = _choice(
        "ANALYTICS_CHAT_SPEED_UNIT",
        pick_setting(
            "ANALYTICS_CHAT_SPEED_UNIT",
            analytics.get("chat_speed_unit"),
            "kmh",
        ),
        _SPEED_UNITS,
    )
    values: dict[str, object] = {"environment": environment}
    values.update(http_settings(analytics))
    values.update(
        source_settings(
            analytics,
            chatsurfer,
            crypto,
            clairvoyant,
            reference,
            loaded,
            mode=mode,
            encryption_cert=encryption_cert,
            clairvoyant_mode=clairvoyant_mode,
            enabled=enabled,
            ws_url=ws_url,
            base_url=base_url,
            speed_unit=speed_unit,
            environment=environment,
        )
    )
    values.update(grid_settings(analytics))
    values.update(map_settings(analytics))
    values.update(coord_settings(analytics))
    values.update(output_coord_settings(analytics))
    values.update(room_settings(analytics))
    settings = AnalyticsSettings(**values)  # type: ignore[arg-type]
    if settings.frontend_enabled and not settings.frontend_ingest_url:
        raise RuntimeError(
            "ANALYTICS_FRONTEND_ENABLED=true requires ANALYTICS_FRONTEND_INGEST_URL."
        )
    if settings.map_provider == "mapbox" and not settings.map_mapbox_token:
        raise RuntimeError(
            "NEXT_PUBLIC_MAP_PROVIDER=mapbox requires MAPBOX_PUBLIC_TOKEN."
        )
    if settings.map_min_zoom > settings.map_max_zoom:
        raise RuntimeError(
            "ANALYTICS_MAP_MIN_ZOOM must be <= ANALYTICS_MAP_MAX_ZOOM."
        )
    return settings
