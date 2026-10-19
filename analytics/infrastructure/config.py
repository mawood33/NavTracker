import os
from dataclasses import dataclass, fields
from pathlib import Path

import yaml

from analytics.application.failover import ClairvoyantFailover
from analytics.domain.assessment import ConfidencePolicy
from analytics.domain.correlation import CorrelationPolicy
from analytics.domain.grid import GridPolicy
from analytics.domain.reports import ReportPolicy
from analytics.domain.routing import (
    MAPBOX_DIRECTIONS_BASE,
    OSRM_PUBLIC_BASE_URL,
    VehicleProfile,
    default_profiles,
)
from analytics.domain.thresholds import AnalysisThresholds
from analytics.infrastructure.reference_parse import ColumnMap
from navtracker_common.crypto import fernet_key_for_mode, require_public_material
from navtracker_common.settings import env_setting, int_setting, pick_setting

_ENVIRONMENTS = ("development", "staging", "production")
_CRYPTO_MODES = {"pem", "ssh", "fernet"}
_SPEED_UNITS = {"mps", "kmh"}
_CLAIRVOYANT_MODES = {"websocket", "poll"}
_MAP_PROVIDERS = {"leaflet", "mapbox", "none", "xyz", "osm"}
_ROUTING_ENGINES = {"msr", "osrm", "valhalla", "graphhopper", "mapbox"}


@dataclass(frozen=True)
class AnalyticsSettings:
    environment: str
    http_host: str
    http_port: int
    reference_dir: str
    publish_events: bool
    analysis_interval_seconds: float
    chat_poll_seconds: float
    chat_speed_unit: str
    read_rooms: tuple[str, ...]
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
    fernet_key: bytes | None
    clairvoyant_enabled: bool
    clairvoyant_mode: str
    clairvoyant_ws_url: str
    clairvoyant_base_url: str
    clairvoyant_api_key: str
    clairvoyant_layer: str
    clairvoyant_failover: ClairvoyantFailover
    thresholds: AnalysisThresholds
    confidence: ConfidencePolicy
    grid: GridPolicy
    report_policy: ReportPolicy
    tacrep_rooms: tuple[str, ...]
    clairvoyant_report_layers: tuple[str, ...]
    map_provider: str
    map_tile_url: str
    map_attribution: str
    map_min_zoom: int
    map_max_zoom: int
    routing_engine: str
    routing_base_url: str
    routing_cache_ttl_seconds: float
    routing_cache_size: int
    routing_snap_meters: float
    routing_join_meters: float
    routing_profiles: tuple[VehicleProfile, ...]
    frontend_enabled: bool
    frontend_ingest_url: str
    public_base_url: str
    columns: ColumnMap
    correlation: CorrelationPolicy = CorrelationPolicy()
    routing_timeout_seconds: float = 15
    routing_mapbox_token: str = ""
    map_center_latitude: float = 39.8283
    map_center_longitude: float = -98.5795
    map_zoom: int = 4
    map_mapbox_token: str = ""
    read_token: str = ""
    write_token: str = ""
    ws_origins: tuple[str, ...] = ()
    max_body_bytes: int = 1_048_576
    max_upload_bytes: int = 8_388_608
    max_ws_bytes: int = 1_048_576
    max_archive_entries: int = 64
    max_archive_bytes: int = 33_554_432
    max_ws_queue: int = 8
    fix_retention_seconds: float = 86_400
    coord_card_path: str = ""
    coord_mismatch_meters: float = 250.0


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
    settings = AnalyticsSettings(
        environment=environment,
        http_host=env_setting(
            "ANALYTICS_HTTP_HOST",
            str(analytics.get("http_host", "127.0.0.1")),
        ),
        http_port=int_setting(
            "ANALYTICS_HTTP_PORT",
            env_setting(
                "ANALYTICS_HTTP_PORT",
                str(analytics.get("http_port", 8080)),
            ),
        ),
        reference_dir=env_setting(
            "ANALYTICS_REFERENCE_DIR",
            str(analytics.get("reference_dir", "")),
        ),
        publish_events=_bool_setting(
            "ANALYTICS_PUBLISH_EVENTS",
            pick_setting(
                "ANALYTICS_PUBLISH_EVENTS",
                analytics.get("publish_events"),
                False,
            ),
        ),
        analysis_interval_seconds=_positive(
            "ANALYTICS_INTERVAL_SECONDS",
            pick_setting(
                "ANALYTICS_INTERVAL_SECONDS",
                analytics.get("analysis_interval_seconds"),
                30,
            ),
        ),
        chat_poll_seconds=_positive(
            "ANALYTICS_CHAT_POLL_SECONDS",
            pick_setting(
                "ANALYTICS_CHAT_POLL_SECONDS",
                analytics.get("chat_poll_seconds"),
                15,
            ),
        ),
        chat_speed_unit=speed_unit,
        read_rooms=_rooms(chatsurfer),
        chatsurfer_base_url=env_setting(
            "CHAT_SURFER_BASE_URL",
            str(chatsurfer.get("base_url", "")),
        ),
        chatsurfer_api_key=env_setting("CHAT_SURFER_API_KEY"),
        chatsurfer_domain_id=env_setting(
            "CHAT_SURFER_DOMAIN_ID",
            str(chatsurfer.get("domain_id", "")),
        ),
        chatsurfer_room_name=env_setting(
            "CHAT_SURFER_ROOM_NAME",
            str(chatsurfer.get("room_name", "")),
        ),
        chatsurfer_room_name_encrypted=env_setting(
            "CHAT_SURFER_ROOM_NAME_ENCRYPTED",
            str(chatsurfer.get("room_name_encrypted", "")),
        ),
        chatsurfer_nickname=env_setting(
            "CHAT_SURFER_NICKNAME",
            str(chatsurfer.get("nickname", "navtracker-analytics")),
        ),
        chatsurfer_classification=env_setting(
            "CHAT_SURFER_CLASSIFICATION",
            str(chatsurfer.get("classification", "U")),
        ),
        chatsurfer_user_high_classification=env_setting(
            "CHAT_SURFER_USER_HIGH_CLASSIFICATION",
            str(chatsurfer.get("user_high_classification", "UNCLASSIFIED//FOUO")),
        ),
        chatsurfer_client_cert=env_setting(
            "CHAT_SURFER_CLIENT_CERT",
            str(chatsurfer.get("client_cert", "")),
        ),
        chatsurfer_client_key=env_setting(
            "CHAT_SURFER_CLIENT_KEY",
            str(chatsurfer.get("client_key", "")),
        ),
        chatsurfer_rate_limit=_positive(
            "CHAT_SURFER_RATE_LIMIT",
            pick_setting(
                "CHAT_SURFER_RATE_LIMIT",
                chatsurfer.get("rate_limit"),
                60,
            ),
        ),
        chatsurfer_max_message_size=int_setting(
            "CHAT_SURFER_MAX_MESSAGE_SIZE",
            env_setting(
                "CHAT_SURFER_MAX_MESSAGE_SIZE",
                str(chatsurfer.get("max_message_size", 4000)),
            ),
        ),
        chatsurfer_encryption_required=_bool_setting(
            "CHAT_SURFER_ENCRYPTION_REQUIRED",
            pick_setting(
                "CHAT_SURFER_ENCRYPTION_REQUIRED",
                chatsurfer.get("encryption_required"),
                False,
            ),
        ),
        encryption_cert=encryption_cert,
        encryption_key=env_setting(
            "ENCRYPTION_KEY",
            str(crypto.get("encryption_key", "")),
        ),
        mode=mode,
        fernet_key=_fernet_key(crypto, mode),
        clairvoyant_enabled=enabled,
        clairvoyant_mode=clairvoyant_mode,
        clairvoyant_ws_url=ws_url,
        clairvoyant_base_url=base_url,
        clairvoyant_api_key=env_setting(
            "CLAIRVOYANT_API_KEY",
            str(clairvoyant.get("api_key", "")),
        ),
        clairvoyant_layer=env_setting(
            "CLAIRVOYANT_LAYER",
            str(clairvoyant.get("layer", "default")),
        ),
        clairvoyant_failover=_failover(clairvoyant),
        thresholds=_thresholds(analytics),
        confidence=_confidence(analytics),
        grid=_grid(analytics),
        report_policy=_report_policy(analytics),
        tacrep_rooms=_string_list(
            "CHAT_SURFER_TACREP_ROOMS",
            chatsurfer.get("tacrep_rooms"),
        ),
        clairvoyant_report_layers=_string_list(
            "CLAIRVOYANT_REPORT_LAYERS",
            clairvoyant.get("report_layers"),
        ),
        map_provider=_map_provider(analytics),
        map_tile_url=_map_tile_url(analytics),
        map_attribution=env_setting(
            "ANALYTICS_MAP_ATTRIBUTION",
            str(_map_block(analytics).get("attribution", "")),
        ),
        map_min_zoom=int(
            _non_negative(
                "ANALYTICS_MAP_MIN_ZOOM",
                pick_setting(
                    "ANALYTICS_MAP_MIN_ZOOM",
                    _map_block(analytics).get("min_zoom"),
                    0,
                ),
            )
        ),
        map_max_zoom=int(
            _non_negative(
                "ANALYTICS_MAP_MAX_ZOOM",
                pick_setting(
                    "ANALYTICS_MAP_MAX_ZOOM",
                    _map_block(analytics).get("max_zoom"),
                    19,
                ),
            )
        ),
        routing_engine=_routing_engine(analytics),
        routing_base_url=_routing_base_url(analytics),
        routing_cache_ttl_seconds=_non_negative(
            "ANALYTICS_ROUTING_CACHE_TTL_SECONDS",
            pick_setting(
                "ANALYTICS_ROUTING_CACHE_TTL_SECONDS",
                _routing_block(analytics).get("cache_ttl_seconds"),
                300,
            ),
        ),
        routing_cache_size=int(
            _positive(
                "ANALYTICS_ROUTING_CACHE_SIZE",
                pick_setting(
                    "ANALYTICS_ROUTING_CACHE_SIZE",
                    _routing_block(analytics).get("cache_size"),
                    256,
                ),
            )
        ),
        routing_snap_meters=_positive(
            "ANALYTICS_ROUTING_SNAP_METERS",
            pick_setting(
                "ANALYTICS_ROUTING_SNAP_METERS",
                _routing_block(analytics).get("snap_meters"),
                500,
            ),
        ),
        routing_join_meters=_non_negative(
            "ANALYTICS_ROUTING_JOIN_METERS",
            pick_setting(
                "ANALYTICS_ROUTING_JOIN_METERS",
                _routing_block(analytics).get("join_meters"),
                40,
            ),
        ),
        routing_profiles=_routing_profiles(analytics),
        frontend_enabled=_bool_setting(
            "ANALYTICS_FRONTEND_ENABLED",
            pick_setting(
                "ANALYTICS_FRONTEND_ENABLED",
                _frontend_block(loaded).get("enabled"),
                False,
            ),
        ),
        frontend_ingest_url=env_setting(
            "ANALYTICS_FRONTEND_INGEST_URL",
            str(_frontend_block(loaded).get("ingest_url", "")),
        ).strip(),
        public_base_url=env_setting(
            "ANALYTICS_PUBLIC_BASE_URL",
            str(analytics.get("public_base_url", "")),
        ).strip(),
        columns=_columns(reference),
        correlation=_correlation(analytics),
        routing_timeout_seconds=_positive(
            "ANALYTICS_ROUTING_TIMEOUT_SECONDS",
            pick_setting(
                "ANALYTICS_ROUTING_TIMEOUT_SECONDS",
                _routing_block(analytics).get("timeout_seconds"),
                15,
            ),
        ),
        routing_mapbox_token=_mapbox_token(_routing_block(analytics)),
        map_center_latitude=_finite(
            "ANALYTICS_MAP_CENTER_LATITUDE",
            pick_setting(
                "ANALYTICS_MAP_CENTER_LATITUDE",
                _map_block(analytics).get("center_latitude"),
                39.8283,
            ),
        ),
        map_center_longitude=_finite(
            "ANALYTICS_MAP_CENTER_LONGITUDE",
            pick_setting(
                "ANALYTICS_MAP_CENTER_LONGITUDE",
                _map_block(analytics).get("center_longitude"),
                -98.5795,
            ),
        ),
        map_zoom=int(
            _non_negative(
                "ANALYTICS_MAP_ZOOM",
                pick_setting(
                    "ANALYTICS_MAP_ZOOM",
                    _map_block(analytics).get("zoom"),
                    4,
                ),
            )
        ),
        map_mapbox_token=_mapbox_token(_map_block(analytics)),
        read_token=env_setting("ANALYTICS_READ_TOKEN", str(analytics.get("read_token", ""))),
        write_token=env_setting("ANALYTICS_WRITE_TOKEN", str(analytics.get("write_token", ""))),
        ws_origins=_ws_origins(analytics),
        max_body_bytes=_byte_setting(
            "ANALYTICS_MAX_BODY_BYTES",
            analytics.get("max_body_bytes"),
            1_048_576,
        ),
        max_upload_bytes=_byte_setting(
            "ANALYTICS_MAX_UPLOAD_BYTES",
            analytics.get("max_upload_bytes"),
            8_388_608,
        ),
        max_ws_bytes=_byte_setting(
            "ANALYTICS_MAX_WS_BYTES",
            analytics.get("max_ws_bytes"),
            1_048_576,
        ),
        max_archive_entries=int(
            _byte_setting(
                "ANALYTICS_MAX_ARCHIVE_ENTRIES",
                analytics.get("max_archive_entries"),
                64,
            )
        ),
        max_archive_bytes=_byte_setting(
            "ANALYTICS_MAX_ARCHIVE_BYTES",
            analytics.get("max_archive_bytes"),
            33_554_432,
        ),
        max_ws_queue=int(
            _byte_setting("ANALYTICS_MAX_WS_QUEUE", analytics.get("max_ws_queue"), 8)
        ),
        fix_retention_seconds=_non_negative(
            "ANALYTICS_FIX_RETENTION_SECONDS",
            pick_setting(
                "ANALYTICS_FIX_RETENTION_SECONDS",
                analytics.get("fix_retention_seconds"),
                86_400,
            ),
        ),
        coord_card_path=env_setting(
            "ANALYTICS_COORD_CARD",
            str(analytics.get("coord_card", "")),
        ).strip(),
        coord_mismatch_meters=_non_negative(
            "ANALYTICS_COORD_MISMATCH_METERS",
            pick_setting(
                "ANALYTICS_COORD_MISMATCH_METERS",
                analytics.get("coord_mismatch_meters"),
                250,
            ),
        ),
    )
    if settings.frontend_enabled and not settings.frontend_ingest_url:
        raise RuntimeError(
            "ANALYTICS_FRONTEND_ENABLED=true requires ANALYTICS_FRONTEND_INGEST_URL."
        )
    if settings.map_provider == "mapbox" and not settings.map_mapbox_token:
        raise RuntimeError(
            "NEXT_PUBLIC_MAP_PROVIDER=mapbox requires NEXT_PUBLIC_MAPBOX_TOKEN."
        )
    if settings.map_min_zoom > settings.map_max_zoom:
        raise RuntimeError(
            "ANALYTICS_MAP_MIN_ZOOM must be <= ANALYTICS_MAP_MAX_ZOOM."
        )
    return settings


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


_INT_THRESHOLDS = {
    "mill_min_vehicles",
    "hide_max_vehicles",
    "transload_min_vehicles",
    "launch_min_visits",
}


def _thresholds(analytics: dict[str, object]) -> AnalysisThresholds:
    defaults = AnalysisThresholds()
    floats: dict[str, float] = {}
    ints: dict[str, int] = {}
    for field in fields(defaults):
        env_name = "ANALYTICS_" + field.name.upper()
        raw = pick_setting(
            env_name,
            analytics.get(field.name),
            getattr(defaults, field.name),
        )
        number = _non_negative(env_name, raw)
        if field.name in _INT_THRESHOLDS:
            if number != int(number):
                raise RuntimeError(
                    f"Invalid {env_name}={raw!r}. Expected an integer."
                )
            ints[field.name] = int(number)
        else:
            floats[field.name] = number
    return AnalysisThresholds(
        dedupe_tolerance_seconds=floats["dedupe_tolerance_seconds"],
        slow_speed_mps=floats["slow_speed_mps"],
        slowdown_ratio=floats["slowdown_ratio"],
        stop_speed_mps=floats["stop_speed_mps"],
        min_dwell_seconds=floats["min_dwell_seconds"],
        mill_radius_meters=floats["mill_radius_meters"],
        mill_min_vehicles=ints["mill_min_vehicles"],
        mill_min_overlap_seconds=floats["mill_min_overlap_seconds"],
        aoi_radius_meters=floats["aoi_radius_meters"],
        hide_min_dwell_seconds=floats["hide_min_dwell_seconds"],
        hide_max_vehicles=ints["hide_max_vehicles"],
        transload_min_vehicles=ints["transload_min_vehicles"],
        transload_min_dwell_seconds=floats["transload_min_dwell_seconds"],
        launch_min_visits=ints["launch_min_visits"],
        launch_min_dwell_seconds=floats["launch_min_dwell_seconds"],
        launch_max_dwell_seconds=floats["launch_max_dwell_seconds"],
        site_match_meters=floats["site_match_meters"],
        msr_corridor_meters=floats["msr_corridor_meters"],
        revisit_gap_seconds=floats["revisit_gap_seconds"],
    )


def _confidence(analytics: dict[str, object]) -> ConfidencePolicy:
    raw = _nested(analytics.get("confidence"), "analytics.confidence")
    defaults = ConfidencePolicy()
    values = _float_fields("ANALYTICS_CONFIDENCE_", raw, defaults)
    policy = ConfidencePolicy(
        initial_confidence=values["initial_confidence"],
        probable_at=values["probable_at"],
        confirmed_at=values["confirmed_at"],
        window_seconds=values["window_seconds"],
        type_evidence_at=values["type_evidence_at"],
        stop_weight=values["stop_weight"],
        dwell_weight_per_minute=values["dwell_weight_per_minute"],
        dwell_cap=values["dwell_cap"],
        mill_weight=values["mill_weight"],
        extra_vehicle_weight=values["extra_vehicle_weight"],
        revisit_weight=values["revisit_weight"],
        msr_transload_weight=values["msr_transload_weight"],
        msr_adjacency_weight=values["msr_adjacency_weight"],
        off_msr_hide_weight=values["off_msr_hide_weight"],
        cell_score_scale=values["cell_score_scale"],
        cell_cap=values["cell_cap"],
        decay_per_day=values["decay_per_day"],
        decay_after_seconds=values["decay_after_seconds"],
        max_confidence=values["max_confidence"],
    )
    if policy.probable_at > policy.confirmed_at:
        raise RuntimeError(
            "ANALYTICS_CONFIDENCE_PROBABLE_AT must be <= "
            "ANALYTICS_CONFIDENCE_CONFIRMED_AT."
        )
    if policy.initial_confidence > policy.max_confidence:
        raise RuntimeError(
            "ANALYTICS_CONFIDENCE_INITIAL_CONFIDENCE must be <= "
            "ANALYTICS_CONFIDENCE_MAX_CONFIDENCE."
        )
    return policy


def _grid(analytics: dict[str, object]) -> GridPolicy:
    raw = _nested(analytics.get("grid"), "analytics.grid")
    defaults = GridPolicy()
    values = _float_fields("ANALYTICS_GRID_", raw, defaults)
    resolution = values["resolution"]
    if resolution != int(resolution) or resolution > 15:
        raise RuntimeError(
            "Invalid ANALYTICS_GRID_RESOLUTION="
            f"{resolution!r}. Expected an integer from 0 to 15."
        )
    return GridPolicy(
        resolution=int(resolution),
        window_seconds=values["window_seconds"],
        decay_half_life_seconds=values["decay_half_life_seconds"],
        hotspot_score=values["hotspot_score"],
        stop_weight=values["stop_weight"],
        dwell_weight_per_minute=values["dwell_weight_per_minute"],
        dwell_cap=values["dwell_cap"],
        extra_vehicle_weight=values["extra_vehicle_weight"],
        mill_weight=values["mill_weight"],
        msr_weight=values["msr_weight"],
    )


def _float_fields(
    prefix: str,
    raw: dict[str, object],
    defaults: ConfidencePolicy | GridPolicy,
) -> dict[str, float]:
    values: dict[str, float] = {}
    for field in fields(defaults):
        env_name = prefix + field.name.upper()
        values[field.name] = _non_negative(
            env_name,
            pick_setting(env_name, raw.get(field.name), getattr(defaults, field.name)),
        )
    return values


def _nested(value: object, name: str) -> dict[str, object]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise RuntimeError(f"{name} must be a mapping.")
    return value


def _report_policy(analytics: dict[str, object]) -> ReportPolicy:
    raw = _nested(analytics.get("reports"), "analytics.reports")
    defaults = ReportPolicy()
    window_seconds = _non_negative(
        "ANALYTICS_REPORTS_WINDOW_SECONDS",
        pick_setting(
            "ANALYTICS_REPORTS_WINDOW_SECONDS",
            raw.get("window_seconds"),
            defaults.window_seconds,
        ),
    )
    weight = _non_negative(
        "ANALYTICS_REPORTS_WEIGHT",
        pick_setting("ANALYTICS_REPORTS_WEIGHT", raw.get("weight"), defaults.weight),
    )
    type_weight = _non_negative(
        "ANALYTICS_REPORTS_TYPE_WEIGHT",
        pick_setting(
            "ANALYTICS_REPORTS_TYPE_WEIGHT",
            raw.get("type_weight"),
            defaults.type_weight,
        ),
    )
    cap = _non_negative(
        "ANALYTICS_REPORTS_CAP",
        pick_setting("ANALYTICS_REPORTS_CAP", raw.get("cap"), defaults.cap),
    )
    return ReportPolicy(
        window_seconds=window_seconds,
        weight=weight,
        type_weight=type_weight,
        cap=cap,
        hide_keywords=_keywords(raw, "hide", defaults.hide_keywords),
        launch_keywords=_keywords(raw, "launch", defaults.launch_keywords),
        transload_keywords=_keywords(raw, "transload", defaults.transload_keywords),
    )


def _keywords(
    raw: dict[str, object],
    site_type: str,
    default: tuple[str, ...],
) -> tuple[str, ...]:
    block = raw.get("keywords")
    if not isinstance(block, dict) or site_type not in block:
        return default
    value = block[site_type]
    if isinstance(value, list):
        return tuple(str(item).strip().lower() for item in value if str(item).strip())
    return tuple(part.strip().lower() for part in str(value).split(",") if part.strip())


def _string_list(env_name: str, yaml_value: object) -> tuple[str, ...]:
    raw = pick_setting(env_name, yaml_value, "")
    if isinstance(raw, list):
        return tuple(str(item).strip() for item in raw if str(item).strip())
    return tuple(part.strip() for part in str(raw).split(",") if part.strip())


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


def _byte_setting(name: str, configured: object, default: int) -> int:
    number = _positive(name, pick_setting(name, configured, default))
    if number != int(number):
        raise RuntimeError(f"Invalid {name}={configured!r}. Expected an integer.")
    return int(number)


def _map_block(analytics: dict[str, object]) -> dict[str, object]:
    return _nested(analytics.get("map"), "analytics.map")


def _routing_block(analytics: dict[str, object]) -> dict[str, object]:
    return _nested(analytics.get("routing"), "analytics.routing")


def _frontend_block(loaded: dict[str, object]) -> dict[str, object]:
    return _nested(loaded.get("frontend"), "frontend")


def _map_provider(analytics: dict[str, object]) -> str:
    public = os.getenv("NEXT_PUBLIC_MAP_PROVIDER", "").strip()
    raw = public or pick_setting(
        "ANALYTICS_MAP_PROVIDER",
        _map_block(analytics).get("provider"),
        "leaflet",
    )
    chosen = _choice("NEXT_PUBLIC_MAP_PROVIDER", raw, _MAP_PROVIDERS)
    if chosen == "osm":
        return "leaflet"
    return chosen


def _map_tile_url(analytics: dict[str, object]) -> str:
    url = env_setting(
        "ANALYTICS_MAP_TILE_URL",
        str(_map_block(analytics).get("tile_url", "")),
    ).strip()
    if _map_provider(analytics) == "xyz" and not url:
        raise RuntimeError(
            "ANALYTICS_MAP_PROVIDER=xyz requires ANALYTICS_MAP_TILE_URL."
        )
    return url


def _routing_engine(analytics: dict[str, object]) -> str:
    routed = os.getenv("ROUTING_PROVIDER", "").strip()
    raw = routed or pick_setting(
        "ANALYTICS_ROUTING_ENGINE",
        _routing_block(analytics).get("engine"),
        "osrm",
    )
    return _choice("ROUTING_PROVIDER", raw, _ROUTING_ENGINES)


def _routing_base_url(analytics: dict[str, object]) -> str:
    engine = _routing_engine(analytics)
    block = _routing_block(analytics)
    yaml_key = {
        "osrm": "osrm_base_url",
        "valhalla": "valhalla_base_url",
        "graphhopper": "graphhopper_base_url",
        "mapbox": "mapbox_base_url",
    }.get(engine, "base_url")
    fallback = ""
    if engine == "osrm":
        fallback = OSRM_PUBLIC_BASE_URL
    elif engine == "mapbox":
        fallback = MAPBOX_DIRECTIONS_BASE
    configured = str(block.get(yaml_key, "") or "")
    url = env_setting("ANALYTICS_ROUTING_BASE_URL", configured or fallback).strip() or fallback
    if engine in {"valhalla", "graphhopper"} and not url:
        raise RuntimeError(
            f"ROUTING_PROVIDER={engine} requires ANALYTICS_ROUTING_BASE_URL."
        )
    if engine == "mapbox" and not _mapbox_token(block):
        raise RuntimeError(
            "ROUTING_PROVIDER=mapbox requires NEXT_PUBLIC_MAPBOX_TOKEN."
        )
    return url


def _mapbox_token(block: dict[str, object]) -> str:
    for name in ("NEXT_PUBLIC_MAPBOX_TOKEN", "ANALYTICS_MAPBOX_TOKEN"):
        value = os.getenv(name, "").strip()
        if value:
            return value
    return str(block.get("mapbox_token", "") or "").strip()


def _correlation(analytics: dict[str, object]) -> CorrelationPolicy:
    raw = _nested(analytics.get("correlation"), "analytics.correlation")
    defaults = CorrelationPolicy()
    sources = _positive(
        "ANALYTICS_CORRELATION_MINIMUM_SOURCES",
        pick_setting(
            "ANALYTICS_CORRELATION_MINIMUM_SOURCES",
            raw.get("minimum_sources"),
            defaults.minimum_sources,
        ),
    )
    if sources != int(sources):
        raise RuntimeError(
            "Invalid ANALYTICS_CORRELATION_MINIMUM_SOURCES. Expected an integer."
        )
    return CorrelationPolicy(
        window_minutes=_positive(
            "ANALYTICS_CORRELATION_WINDOW_MINUTES",
            pick_setting(
                "ANALYTICS_CORRELATION_WINDOW_MINUTES",
                raw.get("window_minutes"),
                defaults.window_minutes,
            ),
        ),
        hide_gap_minutes=_non_negative(
            "ANALYTICS_CORRELATION_HIDE_GAP_MINUTES",
            pick_setting(
                "ANALYTICS_CORRELATION_HIDE_GAP_MINUTES",
                raw.get("hide_gap_minutes"),
                defaults.hide_gap_minutes,
            ),
        ),
        minimum_correlation_score=_non_negative(
            "ANALYTICS_CORRELATION_MINIMUM_SCORE",
            pick_setting(
                "ANALYTICS_CORRELATION_MINIMUM_SCORE",
                raw.get("minimum_score"),
                defaults.minimum_correlation_score,
            ),
        ),
        minimum_sources=int(sources),
        minimum_transload_speed_mps=_non_negative(
            "ANALYTICS_CORRELATION_MINIMUM_TRANSLOAD_SPEED_MPS",
            pick_setting(
                "ANALYTICS_CORRELATION_MINIMUM_TRANSLOAD_SPEED_MPS",
                raw.get("minimum_transload_speed_mps"),
                defaults.minimum_transload_speed_mps,
            ),
        ),
    )


def _finite(name: str, value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise RuntimeError(f"Invalid {name}={value!r}. Expected a number.")
    try:
        return float(value)
    except ValueError:
        raise RuntimeError(f"Invalid {name}={value!r}. Expected a number.") from None


def _routing_profiles(analytics: dict[str, object]) -> tuple[VehicleProfile, ...]:
    raw = _routing_block(analytics).get("profiles")
    if not isinstance(raw, list) or not raw:
        return default_profiles()
    profiles: list[VehicleProfile] = []
    for item in raw:
        if not isinstance(item, dict) or not str(item.get("name", "")).strip():
            raise RuntimeError("routing.profiles entries need a name.")
        name = str(item["name"]).strip()
        baseline = VehicleProfile(name=name)
        profiles.append(
            VehicleProfile(
                name=name,
                max_speed_mps=_positive(
                    "ANALYTICS_ROUTING_PROFILE_MAX_SPEED_MPS",
                    item.get("max_speed_mps", baseline.max_speed_mps),
                ),
                weight_class=str(item.get("weight_class", baseline.weight_class)),
                vehicle_type=str(item.get("vehicle_type", baseline.vehicle_type)),
                eta_factor=_positive(
                    "ANALYTICS_ROUTING_PROFILE_ETA_FACTOR",
                    item.get("eta_factor", baseline.eta_factor),
                ),
                osrm_profile=str(item.get("osrm_profile", baseline.osrm_profile)),
                valhalla_costing=str(
                    item.get("valhalla_costing", baseline.valhalla_costing)
                ),
                graphhopper_profile=str(
                    item.get("graphhopper_profile", baseline.graphhopper_profile)
                ),
            )
        )
    return tuple(profiles)


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


def _mapping(value: object, name: str) -> dict[str, object]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise RuntimeError(f"{name} must be a mapping.")
    return value


def _choice(name: str, value: object, allowed: set[str]) -> str:
    text = str(value).strip().lower()
    if text not in allowed:
        choices = ", ".join(sorted(allowed))
        raise RuntimeError(f"Invalid {name}={value!r}. Expected one of: {choices}.")
    return text


def _bool_setting(name: str, value: object) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "yes"}:
        return True
    if text in {"0", "false", "no", ""}:
        return False
    raise RuntimeError(f"Invalid {name}={value!r}. Expected true or false.")


def _positive(name: str, value: object) -> float:
    number = _non_negative(name, value)
    if number <= 0:
        raise RuntimeError(f"Invalid {name}={value!r}. Expected a number greater than 0.")
    return number


def _non_negative(name: str, value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise RuntimeError(f"Invalid {name}={value!r}. Expected a number.")
    try:
        number = float(value)
    except ValueError:
        raise RuntimeError(f"Invalid {name}={value!r}. Expected a number.") from None
    if number < 0:
        raise RuntimeError(f"Invalid {name}={value!r}. Expected a number >= 0.")
    return number
