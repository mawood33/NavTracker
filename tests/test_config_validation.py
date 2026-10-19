from pathlib import Path

import httpx
import pytest
from app.config_validation import (
    ENVIRONMENTS,
    ConfigValidationError,
    main,
    validate_environment,
)

_NICKNAMES = {
    "development": "navtracker-dev",
    "staging": "navtracker-staging",
    "production": "navtracker",
}
_POLL_INTERVALS = {
    "development": 30,
    "staging": 15,
    "production": 30,
}


def _write_development(tmp_path: Path, text: str) -> Path:
    (tmp_path / "development.yaml").write_text(text, encoding="utf-8")
    return tmp_path


def _development_text() -> str:
    return Path("config/development.yaml").read_text(encoding="utf-8")


@pytest.mark.parametrize("environment", ENVIRONMENTS)
def test_checked_in_config_constructs_without_network(environment):
    result = validate_environment(environment)
    settings = result.settings
    assert settings.environment == environment
    assert settings.navixy_mode.value == "websocket"
    assert settings.mode == "pem"
    assert settings.chatsurfer_nickname == _NICKNAMES[environment]
    assert settings.poll_interval_seconds == _POLL_INTERVALS[environment]
    assert settings.navixy_api_key == "dummy-navixy-key"
    assert settings.chatsurfer_api_key == "dummy-chat-key"
    encryption = environment != "production"
    assert settings.chatsurfer_encryption_required is encryption
    assert result.crypto_handler_built is encryption
    if environment == "development":
        assert settings.tracker_ids == [123456]
    else:
        assert settings.tracker_ids == []


def test_fernet_config_builds_with_a_dummy_key(tmp_path):
    text = _development_text().replace("\n  mode: pem\n", "\n  mode: fernet\n", 1)
    result = validate_environment(
        "development",
        config_dir=_write_development(tmp_path, text),
    )
    assert result.settings.mode == "fernet"
    assert result.settings.fernet_key is not None
    assert result.crypto_handler_built is True


def test_ssh_config_builds_with_a_dummy_public_key(tmp_path):
    text = _development_text().replace("\n  mode: pem\n", "\n  mode: ssh\n", 1)
    result = validate_environment(
        "development",
        config_dir=_write_development(tmp_path, text),
    )
    assert result.settings.mode == "ssh"
    assert result.settings.fernet_key is None
    assert result.crypto_handler_built is True


def test_invalid_mode_names_the_environment_and_setting(tmp_path):
    text = _development_text().replace("\n  mode: pem\n", "\n  mode: nope\n", 1)
    with pytest.raises(ConfigValidationError) as caught:
        validate_environment(
            "development",
            config_dir=_write_development(tmp_path, text),
        )
    assert caught.value.environment == "development"
    assert caught.value.setting == "MODE"
    assert str(caught.value).startswith("APP_ENV=development setting=MODE:")
    assert "Invalid MODE='nope'" in str(caught.value)


def test_invalid_poll_interval_names_the_setting(tmp_path):
    text = _development_text().replace(
        "poll_interval_seconds: 30",
        "poll_interval_seconds: nope",
        1,
    )
    with pytest.raises(ConfigValidationError) as caught:
        validate_environment(
            "development",
            config_dir=_write_development(tmp_path, text),
        )
    assert caught.value.setting == "POLL_INTERVAL_SECONDS"
    assert "APP_ENV=development setting=POLL_INTERVAL_SECONDS:" in str(caught.value)


def test_invalid_failover_attempts_name_the_setting(tmp_path):
    text = _development_text().replace(
        'mode: "websocket"\n',
        'mode: "websocket"\n  failover_max_attempts: 0\n',
        1,
    )
    with pytest.raises(ConfigValidationError) as caught:
        validate_environment(
            "development",
            config_dir=_write_development(tmp_path, text),
        )
    assert caught.value.setting == "NAVIXY_FAILOVER_MAX_ATTEMPTS"
    assert "APP_ENV=development setting=NAVIXY_FAILOVER_MAX_ATTEMPTS:" in str(
        caught.value
    )


def test_zero_rate_limit_names_the_setting(tmp_path):
    text = _development_text().replace("rate_limit: 60", "rate_limit: 0", 1)
    with pytest.raises(ConfigValidationError) as caught:
        validate_environment(
            "development",
            config_dir=_write_development(tmp_path, text),
        )
    assert caught.value.setting == "CHAT_SURFER_RATE_LIMIT"
    assert "APP_ENV=development setting=CHAT_SURFER_RATE_LIMIT:" in str(caught.value)


def test_unknown_environment_names_app_env():
    with pytest.raises(ConfigValidationError) as caught:
        validate_environment("qa")
    assert caught.value.setting == "APP_ENV"
    assert str(caught.value).startswith("APP_ENV=qa setting=APP_ENV:")


def test_missing_config_file_names_app_env(tmp_path):
    with pytest.raises(ConfigValidationError) as caught:
        validate_environment("staging", config_dir=tmp_path)
    assert caught.value.environment == "staging"
    assert caught.value.setting == "APP_ENV"


def test_startup_check_does_not_call_the_network(monkeypatch):
    async def refuse(*args, **kwargs):
        raise AssertionError("network call")

    monkeypatch.setattr("app.infrastructure.navixy_ws.websockets.connect", refuse)
    monkeypatch.setattr(httpx.AsyncClient, "send", refuse)
    monkeypatch.setattr(httpx.AsyncClient, "post", refuse)
    monkeypatch.setattr(httpx.AsyncClient, "request", refuse)
    validate_environment("production")


def test_cli_reports_an_unknown_environment(monkeypatch, capsys):
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    assert main(["qa"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("APP_ENV=qa setting=APP_ENV:")


def test_cli_emits_a_github_warning_for_the_environment_and_setting(
    monkeypatch, capsys
):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    assert main(["qa"]) == 1
    captured = capsys.readouterr()
    assert captured.err.startswith("APP_ENV=qa setting=APP_ENV:")
    assert captured.out.startswith("::warning ")
    assert "title=APP_ENV=qa setting=APP_ENV::" in captured.out
    assert "APP_ENV=qa setting=APP_ENV:" in captured.out
