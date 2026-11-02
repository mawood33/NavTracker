"""TransloadOracle is off unless configured, and a dead URL does not stop analytics."""

import asyncio
import logging

import pytest
from tests.test_analytics_reports import _settings

from analytics.composition import build_analytics
from analytics.infrastructure.config import load_analytics_settings
from analytics.infrastructure.transload_oracle import TransloadOraclePublisher


def test_default_build_disables_transload_oracle(caplog: pytest.LogCaptureFixture):
    caplog.set_level(logging.INFO)
    app = build_analytics(_settings())
    assert app.transload_oracle is None
    health = app.service.health()["transload_oracle"]
    assert isinstance(health, dict)
    assert health["enabled"] is False
    assert health["status"] == "disabled"
    assert health["base_url_configured"] is False
    assert "TransloadOracle integration disabled" in caplog.text


def test_enabled_without_a_url_stays_pull_only(caplog: pytest.LogCaptureFixture):
    caplog.set_level(logging.INFO)
    app = build_analytics(_settings(transload_oracle_enabled=True, transload_oracle_base_url=""))
    assert app.transload_oracle is None
    health = app.service.health()["transload_oracle"]
    assert isinstance(health, dict)
    assert health["status"] == "pull-only"
    assert "no outbound calls" in caplog.text


def test_enabled_publisher_records_success_and_swallows_refusal():
    seen: list[str] = []

    async def post(url: str, body: bytes) -> None:
        seen.append(url)
        assert b"snapshot" in body

    publisher = TransloadOraclePublisher("http://oracle.test/hook", post=post)
    asyncio.run(publisher.publish({"type": "snapshot"}))
    assert publisher.last_result == "ok"
    assert seen == ["http://oracle.test/hook"]

    offline = TransloadOraclePublisher("http://127.0.0.1:9")
    asyncio.run(offline.publish({"type": "snapshot"}))
    assert offline.last_result == "unreachable"


def test_settings_enable_the_client_without_connecting(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("CHAT_SURFER_API_KEY", "dummy")
    monkeypatch.setenv("CHAT_SURFER_ENCRYPTION_REQUIRED", "false")
    monkeypatch.setenv("TRANSLOAD_ORACLE_ENABLED", "true")
    monkeypatch.setenv("TRANSLOAD_ORACLE_BASE_URL", "http://127.0.0.1:9")
    settings = load_analytics_settings()
    assert settings.transload_oracle_enabled is True
    assert settings.transload_oracle_base_url == "http://127.0.0.1:9"
    app = build_analytics(settings)
    assert app.transload_oracle is not None
    assert app.transload_oracle.last_result == ""
    health = app.service.health()["transload_oracle"]
    assert isinstance(health, dict)
    assert health["status"] == "configured"
    assert "127.0.0.1" not in str(health)
