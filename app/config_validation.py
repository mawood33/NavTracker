"""Startup check for the checked-in environment configs.

Loads ``config/<APP_ENV>.yaml``, validates settings, and constructs the
supervisor, Navixy ports, crypto handler, and ChatSurfer publisher. It does
not open a socket or send a request.

API keys are dummies. Crypto material is a dummy for the mode declared in
that file: a PEM or SSH public key when encryption is on, or a Fernet key
when the mode is fernet. The mTLS client cert and key are a local dummy
pair so construction does not read ``/data/certs``. Every other setting
comes from the YAML file.
"""

import asyncio
import os
import sys
import tempfile
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import yaml
from cryptography import x509
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from app.composition import Application, build_application
from app.infrastructure.config import Settings, load_settings

ENVIRONMENTS = ("development", "staging", "production")

# Longest names first so NAVIXY_MODE wins over MODE.
_SETTING_NAMES = (
    "NAVIXY_FAILOVER_BACKOFF_INITIAL_SECONDS",
    "NAVIXY_FAILOVER_PROBE_INTERVAL_SECONDS",
    "NAVIXY_FAILOVER_HEALTHY_WINDOW_SECONDS",
    "NAVIXY_FAILOVER_BACKOFF_CAP_SECONDS",
    "CHAT_SURFER_USER_HIGH_CLASSIFICATION",
    "CHAT_SURFER_ROOM_NAME_ENCRYPTED",
    "CHAT_SURFER_ENCRYPTION_REQUIRED",
    "NAVIXY_FAILOVER_MAX_ATTEMPTS",
    "CHAT_SURFER_MAX_MESSAGE_SIZE",
    "CHAT_SURFER_CLASSIFICATION",
    "CHAT_SURFER_CLIENT_CERT",
    "CHAT_SURFER_CLIENT_KEY",
    "CHAT_SURFER_RATE_LIMIT",
    "CHAT_SURFER_ROOM_NAME",
    "CHAT_SURFER_DOMAIN_ID",
    "CHAT_SURFER_BASE_URL",
    "CHAT_SURFER_API_KEY",
    "CHAT_SURFER_NICKNAME",
    "POLL_INTERVAL_SECONDS",
    "TRACKER_SOURCE_MODE",
    "ENCRYPTION_CERT",
    "ENCRYPTION_KEY",
    "NAVIXY_BASE_URL",
    "NAVIXY_API_KEY",
    "NAVIXY_WS_URL",
    "NAVIXY_MODE",
    "TRACKER_IDS",
    "FERNET_KEY",
    "APP_ENV",
    "MODE",
)

_DUMMY_API_KEY = "dummy-navixy-key"
_DUMMY_CHAT_KEY = "dummy-chat-key"


class ConfigValidationError(RuntimeError):
    def __init__(self, environment: str, setting: str, detail: str):
        self.environment = environment
        self.setting = setting
        self.detail = detail
        super().__init__(f"APP_ENV={environment} setting={setting}: {detail}")


@dataclass(frozen=True)
class StartupCheck:
    settings: Settings
    crypto_handler_built: bool


def validate_environment(
    environment: str,
    *,
    config_dir: str | Path = "config",
) -> StartupCheck:
    """Validate one environment and construct its application objects."""

    if environment not in ENVIRONMENTS:
        raise ConfigValidationError(
            environment,
            "APP_ENV",
            "Expected one of: development, staging, production.",
        )
    root = Path(config_dir)
    mode, encryption_required = _declared_crypto(root, environment)
    with tempfile.TemporaryDirectory(prefix="navtracker-config-check-") as tmp:
        secrets = _dummy_secrets(
            mode,
            encryption_required,
            Path(tmp),
        )
        with _isolated_settings_env(environment, secrets):
            try:
                settings = load_settings(root)
                built = asyncio.run(_build_and_close(settings))
            except ConfigValidationError:
                raise
            except Exception as exc:
                raise _failure(environment, exc) from exc
    return StartupCheck(
        settings=settings,
        crypto_handler_built=built,
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Validate the requested environments. No args checks all three."""

    requested = list(argv) if argv else list(ENVIRONMENTS)
    ok = True
    for environment in requested:
        try:
            validate_environment(environment)
        except ConfigValidationError as exc:
            print(exc, file=sys.stderr)
            ok = False
    if ok:
        print("Config startup check passed: " + ", ".join(requested))
        return 0
    return 1


def _failure(environment: str, exc: BaseException) -> ConfigValidationError:
    detail = str(exc).strip() or exc.__class__.__name__
    return ConfigValidationError(environment, _setting_name(exc), detail)


def _setting_name(exc: BaseException) -> str:
    text = str(exc)
    for name in _SETTING_NAMES:
        if name in text:
            return name
    lowered = text.lower()
    if "rate_limit" in lowered:
        return "CHAT_SURFER_RATE_LIMIT"
    if any(token in lowered for token in ("ssl", "pem lib", "certificate")):
        return "CHAT_SURFER_CLIENT_CERT"
    return "startup"


def _declared_crypto(config_dir: Path, environment: str) -> tuple[str, bool]:
    path = config_dir / f"{environment}.yaml"
    if not path.is_file():
        raise ConfigValidationError(
            environment,
            "APP_ENV",
            f"APP_ENV='{environment}' has no config file at {path}. "
            "Expected one of: development, staging, production.",
        )
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigValidationError(
            environment,
            "APP_ENV",
            f"Could not parse {path}: {exc}",
        ) from exc
    if not isinstance(loaded, dict):
        raise ConfigValidationError(
            environment,
            "APP_ENV",
            f"{path} must be a mapping.",
        )
    crypto = loaded.get("crypto") or {}
    chatsurfer = loaded.get("chatsurfer") or {}
    if not isinstance(crypto, dict):
        raise ConfigValidationError(
            environment,
            "MODE",
            "crypto must be a mapping.",
        )
    if not isinstance(chatsurfer, dict):
        raise ConfigValidationError(
            environment,
            "CHAT_SURFER_ENCRYPTION_REQUIRED",
            "chatsurfer must be a mapping.",
        )
    mode = str(crypto.get("mode", "pem")).strip().lower()
    raw_required = chatsurfer.get("encryption_required", False)
    if isinstance(raw_required, bool):
        required = raw_required
    else:
        required = str(raw_required).strip().lower() == "true"
    return mode, required


def _dummy_secrets(
    mode: str,
    encryption_required: bool,
    directory: Path,
) -> dict[str, str]:
    cert_path, key_path = _write_client_pair(directory)
    secrets = {
        "NAVIXY_API_KEY": _DUMMY_API_KEY,
        "CHAT_SURFER_API_KEY": _DUMMY_CHAT_KEY,
        "CHAT_SURFER_CLIENT_CERT": str(cert_path),
        "CHAT_SURFER_CLIENT_KEY": str(key_path),
    }
    if mode == "fernet":
        secrets["FERNET_KEY"] = Fernet.generate_key().decode("ascii")
        return secrets
    if mode in {"pem", "ssh"} and encryption_required:
        material = directory / ("encryption.pub" if mode == "ssh" else "encryption.pem")
        material.write_bytes(_public_material(mode))
        secrets["ENCRYPTION_CERT"] = str(material)
    return secrets


def _public_material(mode: str) -> bytes:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    if mode == "ssh":
        return key.public_key().public_bytes(
            serialization.Encoding.OpenSSH,
            serialization.PublicFormat.OpenSSH,
        )
    return key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )


def _write_client_pair(directory: Path) -> tuple[Path, Path]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "navtracker-ci")])
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


@contextmanager
def _isolated_settings_env(
    environment: str,
    overrides: dict[str, str],
) -> Iterator[None]:
    """Apply APP_ENV and dummy secrets without leaking the caller's env."""

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


async def _build_and_close(settings: Settings) -> bool:
    app = build_application(settings)
    try:
        _require_constructed(app)
        return app.crypto_handler is not None
    finally:
        await app.aclose()


def _require_constructed(app: Application) -> None:
    if app.supervisor.reader is not app.reader:
        raise RuntimeError("startup: supervisor reader was not the Navixy API port")
    if app.supervisor.stream is not app.stream:
        raise RuntimeError("startup: supervisor stream was not the Navixy websocket port")
    if app.supervisor.publisher is not app.publisher:
        raise RuntimeError("startup: supervisor publisher was not the ChatSurfer client")
