import subprocess
from pathlib import Path

import pytest
from app.application.mode import IngestionMode
from app.infrastructure.config import load_settings
from app.infrastructure.crypto import FERNET_KEY_INVALID, FERNET_KEY_REQUIRED, build_crypto_handler
from cryptography.fernet import Fernet

_SETTINGS_ENV = (
    "APP_ENV",
    "NAVIXY_MODE",
    "TRACKER_IDS",
    "NAVIXY_TRACKER_DISCOVERY_INTERVAL_SECONDS",
    "NAVIXY_BASE_URL",
    "NAVIXY_WS_URL",
    "NAVIXY_API_KEY",
    "NAVIXY_TIMEZONE",
    "POLL_INTERVAL_SECONDS",
    "NAVIXY_FAILOVER_MAX_ATTEMPTS",
    "NAVIXY_FAILOVER_BACKOFF_INITIAL_SECONDS",
    "NAVIXY_FAILOVER_BACKOFF_CAP_SECONDS",
    "NAVIXY_FAILOVER_PROBE_INTERVAL_SECONDS",
    "NAVIXY_FAILOVER_HEALTHY_WINDOW_SECONDS",
    "CHAT_SURFER_BASE_URL",
    "CHAT_SURFER_API_KEY",
    "CHAT_SURFER_DOMAIN_ID",
    "CHAT_SURFER_ROOM_NAME",
    "CHAT_SURFER_ROOM_NAME_ENCRYPTED",
    "CHAT_SURFER_NICKNAME",
    "CHAT_SURFER_CLASSIFICATION",
    "CHAT_SURFER_USER_HIGH_CLASSIFICATION",
    "CHAT_SURFER_CLIENT_CERT",
    "CHAT_SURFER_CLIENT_KEY",
    "CHAT_SURFER_RATE_LIMIT",
    "CHAT_SURFER_MAX_MESSAGE_SIZE",
    "CHAT_SURFER_ENCRYPTION_REQUIRED",
    "ENCRYPTION_CERT",
    "ENCRYPTION_KEY",
    "MODE",
    "FERNET_KEY",
)


def _scrub(monkeypatch):
    for name in _SETTINGS_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("NAVIXY_API_KEY", "navixy-key")
    monkeypatch.setenv("CHAT_SURFER_API_KEY", "chat-key")


def test_pem_mode_treats_yaml_none_as_an_unset_fernet_key(monkeypatch):
    _scrub(monkeypatch)
    settings = load_settings()
    assert settings.mode == "pem"
    assert settings.fernet_key is None
    assert settings.environment == "development"


@pytest.mark.parametrize("mode", ["pem", "ssh"])
@pytest.mark.parametrize("fernet_key", [None, "", "None", "not-a-key"])
def test_pem_and_ssh_do_not_require_or_validate_fernet_key(
    monkeypatch, mode, fernet_key
):
    _scrub(monkeypatch)
    monkeypatch.setenv("MODE", mode)
    if fernet_key is not None:
        monkeypatch.setenv("FERNET_KEY", fernet_key)
    settings = load_settings()
    assert settings.mode == mode
    assert settings.fernet_key is None


@pytest.mark.parametrize("fernet_key", [None, "", "None"])
def test_fernet_mode_rejects_a_missing_key(monkeypatch, fernet_key):
    _scrub(monkeypatch)
    monkeypatch.setenv("MODE", "fernet")
    monkeypatch.setenv("ENCRYPTION_CERT", "")
    if fernet_key is not None:
        monkeypatch.setenv("FERNET_KEY", fernet_key)
    with pytest.raises(RuntimeError, match="fernet mode requires FERNET_KEY") as caught:
        load_settings()
    assert "ENCRYPTION_CERT" not in str(caught.value)


def test_fernet_mode_rejects_the_string_none(monkeypatch):
    _scrub(monkeypatch)
    monkeypatch.setenv("MODE", "  Fernet ")
    monkeypatch.setenv("FERNET_KEY", "None")
    with pytest.raises(RuntimeError, match=FERNET_KEY_REQUIRED):
        load_settings()


def test_fernet_mode_rejects_an_invalid_key(monkeypatch):
    _scrub(monkeypatch)
    monkeypatch.setenv("MODE", "fernet")
    monkeypatch.setenv("FERNET_KEY", "not-a-key")
    with pytest.raises(RuntimeError, match=FERNET_KEY_INVALID):
        load_settings()


@pytest.mark.parametrize("mode", ["pem", "ssh"])
def test_pem_and_ssh_require_encryption_cert_even_with_a_fernet_key(
    monkeypatch, mode
):
    _scrub(monkeypatch)
    monkeypatch.setenv("MODE", mode)
    monkeypatch.setenv("ENCRYPTION_CERT", " ")
    monkeypatch.setenv("FERNET_KEY", Fernet.generate_key().decode("ascii"))
    with pytest.raises(
        RuntimeError, match=f"{mode} mode requires ENCRYPTION_CERT"
    ):
        load_settings()


def test_fernet_mode_does_not_require_encryption_cert(monkeypatch):
    _scrub(monkeypatch)
    key = Fernet.generate_key()
    monkeypatch.setenv("MODE", "fernet")
    monkeypatch.setenv("FERNET_KEY", key.decode("ascii"))
    monkeypatch.setenv("ENCRYPTION_CERT", "")
    settings = load_settings()
    assert settings.fernet_key == key
    assert settings.encryption_cert == ""


def test_invalid_crypto_mode_fails_at_startup(monkeypatch):
    _scrub(monkeypatch)
    monkeypatch.setenv("MODE", "aes")
    with pytest.raises(RuntimeError, match="Invalid MODE='aes'"):
        load_settings()


def test_unknown_app_env_fails_with_the_name(monkeypatch):
    _scrub(monkeypatch)
    monkeypatch.setenv("APP_ENV", "nope")
    with pytest.raises(RuntimeError, match="APP_ENV='nope'"):
        load_settings()


def test_fernet_key_from_env_is_passed_to_the_handler(monkeypatch):
    _scrub(monkeypatch)
    key = Fernet.generate_key()
    monkeypatch.setenv("MODE", "fernet")
    monkeypatch.setenv("FERNET_KEY", f"  {key.decode('ascii')}  ")
    settings = load_settings()

    assert settings.mode == "fernet"
    assert settings.fernet_key == key
    handler = build_crypto_handler(
        encryption_required=settings.chatsurfer_encryption_required,
        mode=settings.mode,
        public_key_path=settings.encryption_cert,
        fernet_key=settings.fernet_key,
    )
    assert handler is not None
    assert handler.fernet_key == key
    assert handler.decrypt(handler.encrypt("wired")) == "wired"


def test_fernet_key_from_yaml_is_loaded(tmp_path, monkeypatch):
    _scrub(monkeypatch)
    key = Fernet.generate_key().decode("ascii")
    source = Path("config/development.yaml").read_text(encoding="utf-8")
    source = source.replace("  mode: pem\n", "  mode: fernet\n")
    source = source.replace("  fernet_key: None\n", f'  fernet_key: "{key}"\n')
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "development.yaml").write_text(source, encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    settings = load_settings()
    assert settings.fernet_key == key.encode("ascii")


def test_app_env_selects_the_production_file(monkeypatch):
    _scrub(monkeypatch)
    monkeypatch.setenv("APP_ENV", "production")
    settings = load_settings()
    assert settings.environment == "production"
    assert settings.navixy_mode is IngestionMode.WEBSOCKET
    assert settings.chatsurfer_encryption_required is False
    assert settings.mode == "pem"
    assert settings.fernet_key is None


def test_compose_passes_app_env_into_the_container():
    text = Path("docker-compose.yml").read_text(encoding="utf-8")
    assert "APP_ENV: ${APP_ENV:-development}" in text


def test_invalid_navixy_timezone_names_the_setting(monkeypatch):
    _scrub(monkeypatch)
    monkeypatch.setenv("NAVIXY_TIMEZONE", "Not/AZone")
    with pytest.raises(RuntimeError, match="Invalid NAVIXY_TIMEZONE='Not/AZone'"):
        load_settings()


def test_env_example_documents_every_setting_and_stays_trackable():
    example = Path(".env.example").read_text(encoding="utf-8")
    for name in _SETTINGS_ENV:
        assert name in example

    ignored = subprocess.run(
        ["git", "check-ignore", "-q", ".env"],
        check=False,
    )
    example_ignored = subprocess.run(
        ["git", "check-ignore", "-q", ".env.example"],
        check=False,
    )
    assert ignored.returncode == 0
    assert example_ignored.returncode == 1
