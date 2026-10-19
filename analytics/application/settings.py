"""Analytics settings. The loader in infrastructure fills this dataclass."""

from dataclasses import dataclass

from analytics.application.failover import ClairvoyantFailover
from analytics.domain.assessment import ConfidencePolicy
from analytics.domain.columns import ColumnMap
from analytics.domain.correlation import CorrelationPolicy
from analytics.domain.grid import GridPolicy
from analytics.domain.reports import ReportPolicy
from analytics.domain.routing import VehicleProfile
from analytics.domain.thresholds import AnalysisThresholds


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
    ellipse_unit: str = "nmi"
    magnetic_declination_degrees: float = 0.0
    cat_ce: tuple[tuple[int, float], ...] = ()
    confidence_terms: tuple[object, ...] = ()
    room_ingest: tuple[tuple[str, tuple[str, ...]], ...] = ()
    room_publish: tuple[tuple[str, tuple[str, ...]], ...] = ()
    default_ingest_rooms: tuple[str, ...] = ()
    default_publish_rooms: tuple[str, ...] = ()
    publish_rules_text: str = ""
    republish_on_higher_level: bool = True
    vacant_evidence_weight: float = 0.25
