import pytest
from app.application.mode import (
    DEFAULT_BACKOFF_CAP_SECONDS,
    DEFAULT_BACKOFF_INITIAL_SECONDS,
    DEFAULT_HEALTHY_WINDOW_SECONDS,
    DEFAULT_MAX_ATTEMPTS,
    DEFAULT_PROBE_INTERVAL_SECONDS,
    IngestionMode,
    build_failover_policy,
    parse_navixy_mode,
    resolve_navixy_mode,
)
from app.infrastructure.config import load_settings


def test_unset_mode_defaults_to_websocket():
    assert resolve_navixy_mode(None, None) is IngestionMode.WEBSOCKET
    assert resolve_navixy_mode("  ", None) is IngestionMode.WEBSOCKET
    assert resolve_navixy_mode(None, "  ") is IngestionMode.WEBSOCKET


def test_poll_and_websocket_are_case_insensitive():
    assert resolve_navixy_mode("poll", None) is IngestionMode.POLL
    assert resolve_navixy_mode("  WEBSOCKET ", "poll") is IngestionMode.WEBSOCKET
    assert parse_navixy_mode("Poll") is IngestionMode.POLL


def test_yaml_mode_used_when_env_blank():
    assert resolve_navixy_mode(None, "poll") is IngestionMode.POLL
    assert resolve_navixy_mode("", "websocket") is IngestionMode.WEBSOCKET


def test_invalid_mode_fails_fast():
    with pytest.raises(RuntimeError, match="Invalid NAVIXY_MODE='stream'"):
        parse_navixy_mode("stream")
    with pytest.raises(RuntimeError, match="Expected one of: websocket, poll"):
        resolve_navixy_mode("socket", "poll")


def test_backoff_is_exponential_and_capped():
    policy = build_failover_policy(
        max_attempts=5,
        backoff_initial_seconds=1,
        backoff_cap_seconds=3,
        probe_interval_seconds=60,
        healthy_window_seconds=30,
    )
    assert policy.backoff_seconds(1) == 1
    assert policy.backoff_seconds(2) == 2
    assert policy.backoff_seconds(3) == 3
    assert policy.backoff_seconds(4) == 3


def test_failover_defaults_and_rejection():
    policy = build_failover_policy()
    assert policy.max_attempts == DEFAULT_MAX_ATTEMPTS
    assert policy.backoff_initial_seconds == DEFAULT_BACKOFF_INITIAL_SECONDS
    assert policy.backoff_cap_seconds == DEFAULT_BACKOFF_CAP_SECONDS
    assert policy.probe_interval_seconds == DEFAULT_PROBE_INTERVAL_SECONDS
    assert policy.healthy_window_seconds == DEFAULT_HEALTHY_WINDOW_SECONDS

    with pytest.raises(RuntimeError, match="NAVIXY_FAILOVER_MAX_ATTEMPTS"):
        build_failover_policy(max_attempts=0)
    with pytest.raises(RuntimeError, match="BACKOFF_CAP_SECONDS"):
        build_failover_policy(
            backoff_initial_seconds=10,
            backoff_cap_seconds=5,
        )


def _scrub_env(monkeypatch):
    for name in (
        "NAVIXY_MODE",
        "TRACKER_SOURCE_MODE",
        "TRACKER_IDS",
        "NAVIXY_FAILOVER_MAX_ATTEMPTS",
        "NAVIXY_FAILOVER_BACKOFF_INITIAL_SECONDS",
        "NAVIXY_FAILOVER_BACKOFF_CAP_SECONDS",
        "NAVIXY_FAILOVER_PROBE_INTERVAL_SECONDS",
        "NAVIXY_FAILOVER_HEALTHY_WINDOW_SECONDS",
        "APP_ENV",
        "MODE",
        "FERNET_KEY",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("NAVIXY_API_KEY", "navixy-key")
    monkeypatch.setenv("CHAT_SURFER_API_KEY", "chat-key")


def test_load_settings_uses_yaml_mode_when_env_unset(monkeypatch):
    _scrub_env(monkeypatch)
    settings = load_settings()
    assert settings.environment == "development"
    assert settings.navixy_mode is IngestionMode.WEBSOCKET
    assert settings.failover.max_attempts == DEFAULT_MAX_ATTEMPTS


def test_load_settings_uses_websocket_for_staging(monkeypatch):
    _scrub_env(monkeypatch)
    monkeypatch.setenv("APP_ENV", "staging")
    settings = load_settings()
    assert settings.environment == "staging"
    assert settings.navixy_mode is IngestionMode.WEBSOCKET


def test_load_settings_env_overrides_yaml(monkeypatch):
    _scrub_env(monkeypatch)
    monkeypatch.setenv("NAVIXY_MODE", "websocket")
    monkeypatch.setenv("NAVIXY_FAILOVER_MAX_ATTEMPTS", "2")
    settings = load_settings()
    assert settings.navixy_mode is IngestionMode.WEBSOCKET
    assert settings.failover.max_attempts == 2


def test_load_settings_rejects_invalid_navixy_mode(monkeypatch):
    _scrub_env(monkeypatch)
    monkeypatch.setenv("NAVIXY_MODE", "stream")
    with pytest.raises(RuntimeError, match="Invalid NAVIXY_MODE='stream'"):
        load_settings()


def test_load_settings_defaults_to_websocket_when_mode_omitted(
    tmp_path, monkeypatch
):
    _scrub_env(monkeypatch)
    with open("config/development.yaml", encoding="utf-8") as source:
        text = source.read().replace('  mode: "websocket"\n', "")
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "development.yaml").write_text(text, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    settings = load_settings()
    assert settings.navixy_mode is IngestionMode.WEBSOCKET


def test_load_settings_rejects_legacy_source_mode(monkeypatch):
    _scrub_env(monkeypatch)
    monkeypatch.setenv("TRACKER_SOURCE_MODE", "poll")
    with pytest.raises(RuntimeError, match="TRACKER_SOURCE_MODE"):
        load_settings()
