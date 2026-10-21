"""File and stdout logging for the worker and analytics."""

import logging
from pathlib import Path

import pytest

from navtracker_common.logging_setup import configure_logging


def _restore_root() -> tuple[list[logging.Handler], int]:
    root = logging.getLogger()
    return list(root.handlers), root.level


def _reset(previous: list[logging.Handler], level: int) -> None:
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()
    for handler in previous:
        root.addHandler(handler)
    root.setLevel(level)


def test_log_file_and_stdout_include_tracebacks(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    previous, level = _restore_root()
    monkeypatch.delenv("LOG_FILE", raising=False)
    monkeypatch.delenv("LOG_MAX_BYTES", raising=False)
    monkeypatch.delenv("LOG_BACKUP_COUNT", raising=False)
    monkeypatch.setenv("LOG_LEVEL", "INFO")
    path = tmp_path / "navtracker.log"
    try:
        configure_logging(str(path))
        try:
            raise TypeError("can't compare offset-naive and offset-aware datetimes")
        except TypeError:
            logging.getLogger("app.application.supervisor").exception(
                "Publication handling failed tracker_id=3617458"
            )
        logging.getLogger("analytics").warning("analytics line")
    finally:
        _reset(previous, level)
    text = path.read_text(encoding="utf-8")
    assert "Publication handling failed tracker_id=3617458" in text
    assert "Traceback" in text
    assert "TypeError" in text
    assert "analytics line" in text
    captured = capsys.readouterr()
    assert "Publication handling failed tracker_id=3617458" in captured.out
    assert "Traceback" in captured.out
    assert "analytics line" in captured.out


def test_blank_log_file_disables_the_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    previous, level = _restore_root()
    monkeypatch.setenv("LOG_FILE", "  ")
    path = tmp_path / "unused.log"
    try:
        configure_logging(str(path))
        logging.getLogger("app").info("stdout only")
    finally:
        _reset(previous, level)
    assert not path.exists()
    assert "stdout only" in capsys.readouterr().out


def test_rotation_keeps_a_backup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    previous, level = _restore_root()
    monkeypatch.delenv("LOG_FILE", raising=False)
    monkeypatch.setenv("LOG_MAX_BYTES", "80")
    monkeypatch.setenv("LOG_BACKUP_COUNT", "1")
    path = tmp_path / "analytics.log"
    try:
        configure_logging(str(path))
        logger = logging.getLogger("analytics.main")
        for index in range(20):
            logger.info("rotation line %s %s", index, "x" * 40)
    finally:
        _reset(previous, level)
    assert path.is_file()
    assert Path(str(path) + ".1").is_file()


def test_invalid_log_settings_name_the_variable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    previous, level = _restore_root()
    try:
        monkeypatch.setenv("LOG_LEVEL", "loud")
        with pytest.raises(RuntimeError, match="LOG_LEVEL"):
            configure_logging(str(tmp_path / "a.log"))
        monkeypatch.delenv("LOG_LEVEL", raising=False)
        monkeypatch.setenv("LOG_MAX_BYTES", "nope")
        with pytest.raises(RuntimeError, match="LOG_MAX_BYTES"):
            configure_logging(str(tmp_path / "a.log"))
        monkeypatch.setenv("LOG_MAX_BYTES", "-1")
        with pytest.raises(RuntimeError, match="LOG_MAX_BYTES"):
            configure_logging(str(tmp_path / "a.log"))
        monkeypatch.delenv("LOG_MAX_BYTES", raising=False)
        monkeypatch.setenv("LOG_BACKUP_COUNT", "1.5")
        with pytest.raises(RuntimeError, match="LOG_BACKUP_COUNT"):
            configure_logging(str(tmp_path / "a.log"))
    finally:
        _reset(previous, level)
