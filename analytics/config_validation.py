"""Startup check for the analytics configs. Does not open a connection."""

import os
import sys
import tempfile
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import yaml
from cryptography import x509
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from analytics.composition import build_analytics
from analytics.infrastructure.config import AnalyticsSettings, load_analytics_settings

ENVIRONMENTS = ("development", "staging", "production")

_SETTING_NAMES = (
    "ANALYTICS_OUTPUT_LATLON_HEMISPHERE",
    "ANALYTICS_OUTPUT_LATLON_SEPARATOR",
    "ANALYTICS_OUTPUT_LATLON_DECIMALS",
    "ANALYTICS_OUTPUT_MGRS_PRECISION_M",
    "ANALYTICS_OUTPUT_COORD_FORMAT",
    "ANALYTICS_OUTPUT_MGRS_SPACES",
    "CHAT_SURFER_USER_HIGH_CLASSIFICATION",
    "CHAT_SURFER_ROOM_NAME_ENCRYPTED",
    "CHAT_SURFER_ENCRYPTION_REQUIRED",
    "CLAIRVOYANT_FAILOVER_BACKOFF_INITIAL_SECONDS",
    "CLAIRVOYANT_FAILOVER_PROBE_INTERVAL_SECONDS",
    "CLAIRVOYANT_FAILOVER_HEALTHY_WINDOW_SECONDS",
    "CLAIRVOYANT_FAILOVER_BACKOFF_CAP_SECONDS",
    "CLAIRVOYANT_FAILOVER_MAX_ATTEMPTS",
    "ANALYTICS_CHAT_SPEED_UNIT",
    "ANALYTICS_REFERENCE_DIR",
    "ANALYTICS_PUBLISH_EVENTS",
    "ANALYTICS_HTTP_PORT",
    "CHAT_SURFER_MAX_MESSAGE_SIZE",
    "CHAT_SURFER_CLASSIFICATION",
    "CHAT_SURFER_CLIENT_CERT",
    "CHAT_SURFER_CLIENT_KEY",
    "CHAT_SURFER_FAILOVER_BACKOFF_INITIAL_SECONDS",
    "CHAT_SURFER_FAILOVER_PROBE_INTERVAL_SECONDS",
    "CHAT_SURFER_FAILOVER_BACKOFF_CAP_SECONDS",
    "CHAT_SURFER_FAILOVER_MAX_ATTEMPTS",
    "CHAT_SURFER_SCRUB_POLL_SECONDS",
    "CHAT_SURFER_BACKFILL_MINUTES",
    "CHAT_SURFER_READ_PAGE_SIZE",
    "CHAT_SURFER_READ_PAGE_PATH",
    "CHAT_SURFER_EXERCISE_ONLY",
    "CHAT_SURFER_INGEST_MODE",
    "CHAT_SURFER_READ_ENABLED",
    "CHAT_SURFER_READ_PATH",
    "CHAT_SURFER_SEEN_LIMIT",
    "CHAT_SURFER_SKIP_BOTS",
    "CHAT_SURFER_READ_ROOMS",
    "CHAT_SURFER_RATE_LIMIT",
    "CHAT_SURFER_ROOM_NAME",
    "CHAT_SURFER_DOMAIN_ID",
    "CHAT_SURFER_BASE_URL",
    "CHAT_SURFER_API_KEY",
    "CHAT_SURFER_NICKNAME",
    "CLAIRVOYANT_BASE_URL",
    "CLAIRVOYANT_API_KEY",
    "CLAIRVOYANT_ENABLED",
    "CLAIRVOYANT_WS_URL",
    "CLAIRVOYANT_LAYER",
    "CLAIRVOYANT_MODE",
    "ENCRYPTION_CERT",
    "ENCRYPTION_KEY",
    "FERNET_KEY",
    "APP_ENV",
    "MODE",
    "CHAT_SURFER_TACREP_ROOMS",
    "CLAIRVOYANT_REPORT_LAYERS",
    "ANALYTICS_MAP_PROVIDER",
    "ANALYTICS_MAP_TILE_URL",
    "ANALYTICS_MAP_ATTRIBUTION",
    "ANALYTICS_MAP_MIN_ZOOM",
    "ANALYTICS_MAP_MAX_ZOOM",
    "ANALYTICS_ROUTING_ENGINE",
    "ANALYTICS_ROUTING_BASE_URL",
    "ANALYTICS_ROUTING_CACHE_TTL_SECONDS",
    "ANALYTICS_ROUTING_CACHE_SIZE",
    "ANALYTICS_ROUTING_SNAP_METERS",
    "ANALYTICS_ROUTING_JOIN_METERS",
    "ANALYTICS_FRONTEND_ENABLED",
    "ANALYTICS_FRONTEND_INGEST_URL",
    "ANALYTICS_PUBLIC_BASE_URL",
    "ANALYTICS_REPORTS_WINDOW_SECONDS",
    "ANALYTICS_REPORTS_WEIGHT",
    "ANALYTICS_REPORTS_TYPE_WEIGHT",
    "ANALYTICS_REPORTS_CAP",
    "ANALYTICS_CONFIDENCE_INITIAL_CONFIDENCE",
    "ANALYTICS_CONFIDENCE_PROBABLE_AT",
    "ANALYTICS_CONFIDENCE_CONFIRMED_AT",
    "ANALYTICS_CONFIDENCE_WINDOW_SECONDS",
    "ANALYTICS_CONFIDENCE_DECAY_PER_DAY",
    "ANALYTICS_GRID_RESOLUTION",
    "ANALYTICS_GRID_WINDOW_SECONDS",
    "ANALYTICS_GRID_DECAY_HALF_LIFE_SECONDS",
    "ANALYTICS_GRID_HOTSPOT_SCORE",
    "NEXT_PUBLIC_MAP_PROVIDER",
    "NEXT_PUBLIC_MAPBOX_TOKEN",
    "ANALYTICS_MAPBOX_TOKEN",
    "MAPBOX_PUBLIC_TOKEN",
    "MAPBOX_SERVER_TOKEN",
    "ROUTING_PROVIDER",
    "ANALYTICS_ROUTING_TIMEOUT_SECONDS",
    "ANALYTICS_CORRELATION_WINDOW_MINUTES",
    "ANALYTICS_CORRELATION_HIDE_GAP_MINUTES",
    "ANALYTICS_CORRELATION_MINIMUM_SCORE",
    "ANALYTICS_CORRELATION_MINIMUM_SOURCES",
    "ANALYTICS_CORRELATION_MINIMUM_TRANSLOAD_SPEED_MPS",
    "ANALYTICS_MAP_CENTER_LATITUDE",
    "ANALYTICS_MAP_CENTER_LONGITUDE",
    "ANALYTICS_MAP_ZOOM",
    "ANALYTICS_HTTP_HOST",
    "ANALYTICS_READ_TOKEN",
    "ANALYTICS_WRITE_TOKEN",
    "ANALYTICS_WS_ORIGINS",
    "ANALYTICS_MAX_BODY_BYTES",
    "ANALYTICS_MAX_UPLOAD_BYTES",
    "ANALYTICS_MAX_WS_BYTES",
    "ANALYTICS_MAX_ARCHIVE_ENTRIES",
    "ANALYTICS_MAX_ARCHIVE_BYTES",
    "ANALYTICS_MAX_WS_QUEUE",
    "ANALYTICS_MAX_FIXES_PER_VEHICLE",
    "ANALYTICS_FIX_RETENTION_SECONDS",
    "ANALYTICS_COORD_CARD_SCHEMA",
    "ANALYTICS_COORD_CARD",
    "ANALYTICS_COORD_MISMATCH_METERS",
    "ANALYTICS_CORRELATION_NEIGHBOR_K",
    "ANALYTICS_MAGNETIC_DECLINATION",
    "ANALYTICS_ELLIPSE_UNIT",
    "ANALYTICS_CORRELATION_EXPLICIT_LINK_WEIGHT",
    "ANALYTICS_CAT_CE",
    "ANALYTICS_CONFIDENCE_TERMS",
    "ANALYTICS_ROOMS_DEFAULT_INGEST",
    "ANALYTICS_ROOMS_DEFAULT_PUBLISH",
    "ANALYTICS_PUBLISH_RULES",
    "ANALYTICS_PUBLISH_REPUBLISH_ON_HIGHER_LEVEL",
    "ANALYTICS_VACANT_EVIDENCE_WEIGHT",
)


class AnalyticsConfigError(RuntimeError):
    def __init__(self, environment: str, setting: str, detail: str):
        self.environment = environment
        self.setting = setting
        self.detail = detail
        super().__init__(f"APP_ENV={environment} setting={setting}: {detail}")


def validate_analytics_environment(
    environment: str,
    *,
    config_dir: str | Path = "config/analytics",
) -> AnalyticsSettings:
    if environment not in ENVIRONMENTS:
        raise AnalyticsConfigError(
            environment,
            "APP_ENV",
            "Expected one of: development, staging, production.",
        )
    root = Path(config_dir)
    mode, encryption_required = _declared(root, environment)
    with tempfile.TemporaryDirectory(prefix="analytics-config-check-") as tmp:
        secrets = _secrets(mode, encryption_required, Path(tmp))
        with _isolated(environment, secrets):
            try:
                settings = load_analytics_settings(root)
                app = build_analytics(settings)
            except AnalyticsConfigError:
                raise
            except Exception as exc:
                raise _failure(environment, exc) from exc
            built = app
    return built.settings


def deployment_warnings(settings: AnalyticsSettings) -> tuple[str, ...]:
    """Staging and production notes. They do not fail the process."""

    if settings.environment not in {"staging", "production"}:
        return ()
    found: list[str] = []
    if not settings.read_token or not settings.write_token:
        found.append(f"APP_ENV={settings.environment} analytics read/write tokens are unset")
    public_osrm = "router.project-osrm.org" in settings.routing_base_url
    if settings.routing_engine == "osrm" or public_osrm:
        found.append(f"APP_ENV={settings.environment} routing uses public OSRM")
    if settings.map_provider in {"leaflet", "osm"}:
        found.append(f"APP_ENV={settings.environment} basemap uses public OSM tiles")
    placeholder = "clairvoyant.example" in settings.clairvoyant_base_url or (
        "clairvoyant.example" in settings.clairvoyant_ws_url
    )
    if settings.clairvoyant_enabled and (placeholder or not settings.clairvoyant_api_key):
        found.append(
            f"APP_ENV={settings.environment} Clairvoyant uses a placeholder endpoint or key"
        )
    return tuple(found)


def main(argv: Sequence[str] | None = None) -> int:
    requested = list(argv) if argv else list(ENVIRONMENTS)
    ok = True
    for environment in requested:
        try:
            settings = validate_analytics_environment(environment)
        except AnalyticsConfigError as exc:
            print(exc, file=sys.stderr)
            _emit_github_warning(exc)
            ok = False
            continue
        for warning in deployment_warnings(settings):
            print(warning, file=sys.stderr)
            _emit_github_warning(
                AnalyticsConfigError(settings.environment, "deployment", warning)
            )
    if ok:
        print("Analytics config check passed: " + ", ".join(requested))
        return 0
    return 1


def _failure(environment: str, exc: BaseException) -> AnalyticsConfigError:
    detail = str(exc).strip() or exc.__class__.__name__
    text = str(exc)
    setting = "startup"
    for name in _SETTING_NAMES:
        if name in text:
            setting = name
            break
    return AnalyticsConfigError(environment, setting, detail)


def _declared(config_dir: Path, environment: str) -> tuple[str, bool]:
    path = config_dir / f"{environment}.yaml"
    if not path.is_file():
        raise AnalyticsConfigError(
            environment,
            "APP_ENV",
            f"APP_ENV='{environment}' has no analytics config at {path}.",
        )
    loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(loaded, dict):
        raise AnalyticsConfigError(environment, "APP_ENV", f"{path} must be a mapping.")
    crypto = loaded.get("crypto") or {}
    chatsurfer = loaded.get("chatsurfer") or {}
    mode = str(crypto.get("mode", "pem")).strip().lower()
    required = chatsurfer.get("encryption_required", False)
    if not isinstance(required, bool):
        required = str(required).strip().lower() == "true"
    return mode, required


def _secrets(mode: str, encryption_required: bool, directory: Path) -> dict[str, str]:
    cert, key = _client_pair(directory)
    secrets = {
        "CHAT_SURFER_API_KEY": "dummy-chat-key",
        "CLAIRVOYANT_API_KEY": "dummy-clairvoyant-key",
        "CHAT_SURFER_CLIENT_CERT": str(cert),
        "CHAT_SURFER_CLIENT_KEY": str(key),
    }
    if mode == "fernet":
        secrets["FERNET_KEY"] = Fernet.generate_key().decode("ascii")
        return secrets
    if mode in {"pem", "ssh"} and encryption_required:
        private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        public_path = directory / ("encryption.pub" if mode == "ssh" else "encryption.pem")
        private_path = directory / "encryption.key"
        if mode == "ssh":
            public_bytes = private.public_key().public_bytes(
                serialization.Encoding.OpenSSH,
                serialization.PublicFormat.OpenSSH,
            )
            private_bytes = private.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.OpenSSH,
                serialization.NoEncryption(),
            )
        else:
            public_bytes = private.public_key().public_bytes(
                serialization.Encoding.PEM,
                serialization.PublicFormat.SubjectPublicKeyInfo,
            )
            private_bytes = private.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
        public_path.write_bytes(public_bytes)
        private_path.write_bytes(private_bytes)
        secrets["ENCRYPTION_CERT"] = str(public_path)
        secrets["ENCRYPTION_KEY"] = str(private_path)
    return secrets


def _client_pair(directory: Path) -> tuple[Path, Path]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "analytics-ci")])
    now = datetime.now(UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    cert_path = directory / "client.crt"
    key_path = directory / "client.key"
    cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return cert_path, key_path


def _emit_github_warning(exc: AnalyticsConfigError) -> None:
    if os.getenv("GITHUB_ACTIONS") != "true":
        return
    title = (
        f"APP_ENV={exc.environment} setting={exc.setting}"
        .replace("%", "%25")
        .replace("\r", "%0D")
        .replace("\n", "%0A")
        .replace(":", "%3A")
        .replace(",", "%2C")
    )
    message = str(exc).replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    print(f"::warning title={title}::{message}", flush=True)


@contextmanager
def _isolated(environment: str, overrides: dict[str, str]) -> Iterator[None]:
    names = set(_SETTING_NAMES) | set(overrides)
    previous = {name: os.environ.get(name) for name in names}
    try:
        for name in names:
            os.environ.pop(name, None)
        os.environ["APP_ENV"] = environment
        os.environ.update(overrides)
        yield
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
