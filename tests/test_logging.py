"""File and stdout logging for the worker and analytics."""

import logging
from pathlib import Path

import pytest

from navtracker_common.logging_setup import configure_logging


def _restore_root() -> tuple[list[logging.Handler], int, int]:
    root = logging.getLogger()
    positions = logging.getLogger("navtracker.positions")
    return list(root.handlers), root.level, positions.level


def _reset(previous: list[logging.Handler], level: int, positions_level: int) -> None:
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()
    for handler in previous:
        root.addHandler(handler)
    root.setLevel(level)
    logging.getLogger("navtracker.positions").setLevel(positions_level)


def test_log_file_and_stdout_include_tracebacks(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    previous, level, positions_level = _restore_root()
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
        _reset(previous, level, positions_level)
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
    previous, level, positions_level = _restore_root()
    monkeypatch.setenv("LOG_FILE", "  ")
    path = tmp_path / "unused.log"
    try:
        configure_logging(str(path))
        logging.getLogger("app").info("stdout only")
    finally:
        _reset(previous, level, positions_level)
    assert not path.exists()
    assert "stdout only" in capsys.readouterr().out


def test_rotation_keeps_a_backup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    previous, level, positions_level = _restore_root()
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
        _reset(previous, level, positions_level)
    assert path.is_file()
    assert Path(str(path) + ".1").is_file()


def test_invalid_log_settings_name_the_variable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    previous, level, positions_level = _restore_root()
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
        monkeypatch.delenv("LOG_BACKUP_COUNT", raising=False)
        monkeypatch.setenv("LOG_POSITIONS", "loud")
        with pytest.raises(RuntimeError, match="LOG_POSITIONS"):
            configure_logging(str(tmp_path / "a.log"))
    finally:
        _reset(previous, level, positions_level)


def test_position_lines_go_to_the_file_and_stdout(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from datetime import UTC, datetime

    from app.application.position_log import log_position
    from app.domain.entities import TrackerPosition

    previous, level, positions_level = _restore_root()
    monkeypatch.delenv("LOG_FILE", raising=False)
    monkeypatch.delenv("LOG_POSITIONS", raising=False)
    monkeypatch.setenv("LOG_LEVEL", "INFO")
    path = tmp_path / "navtracker.log"
    position = TrackerPosition(
        tracker_id=3617458,
        latitude=10.5,
        longitude=-20.25,
        timestamp=datetime(2026, 1, 1, tzinfo=UTC),
        received_timestamp=datetime(2026, 1, 1),
        speed=1.5,
        heading=90.0,
        connection_status="super-secret-api-key",
    )
    try:
        configure_logging(str(path))
        log_position(
            position,
            source="websocket",
            compared_to=datetime(2026, 1, 1),
            normalized=datetime(2026, 1, 1, tzinfo=UTC),
            decision="publish",
        )
    finally:
        _reset(previous, level, positions_level)
    text = path.read_text(encoding="utf-8")
    captured = capsys.readouterr().out
    for sink in (text, captured):
        assert "navtracker.positions" in sink
        assert "tracker_id=3617458" in sink
        assert "source=websocket" in sink
        assert "raw_timestamp=2026-01-01T00:00:00" in sink
        assert "raw_type=datetime" in sink
        assert "raw_tzinfo=None" in sink
        assert "normalized_utc=2026-01-01T00:00:00Z" in sink
        assert "latitude=10.5" in sink
        assert "longitude=-20.25" in sink
        assert "speed=1.5" in sink
        assert "heading=90.0" in sink
        assert "previous_type=datetime" in sink
        assert "previous_tzinfo=None" in sink
        assert "previous_utc=2026-01-01T00:00:00Z" in sink
        assert "decision=publish" in sink
        assert "super-secret-api-key" not in sink


def test_false_hides_positions_until_the_logger_is_debug(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from datetime import UTC, datetime

    from app.application.position_log import log_position
    from app.domain.entities import TrackerPosition

    previous, level, positions_level = _restore_root()
    monkeypatch.setenv("LOG_POSITIONS", "false")
    monkeypatch.setenv("LOG_LEVEL", "INFO")
    path = tmp_path / "navtracker.log"
    position = TrackerPosition(
        tracker_id=4,
        latitude=1.0,
        longitude=2.0,
        timestamp=datetime(2026, 1, 1, tzinfo=UTC),
    )
    try:
        configure_logging(str(path))
        log_position(
            position,
            source="poll",
            compared_to=None,
            normalized=position.timestamp,
            decision="publish",
        )
        quiet = path.read_text(encoding="utf-8")
        logging.getLogger("navtracker.positions").setLevel(logging.DEBUG)
        log_position(
            position,
            source="poll",
            compared_to=None,
            normalized=position.timestamp,
            decision="duplicate",
        )
    finally:
        _reset(previous, level, positions_level)
    assert "tracker_id=4" not in quiet
    text = path.read_text(encoding="utf-8")
    assert "decision=duplicate" in text
    assert "source=poll" in text
    assert "decision=duplicate" in capsys.readouterr().out


def test_debug_positions_ignore_a_warning_root(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from datetime import UTC, datetime

    from app.application.position_log import log_position
    from app.domain.entities import TrackerPosition

    previous, level, positions_level = _restore_root()
    monkeypatch.setenv("LOG_LEVEL", "WARNING")
    monkeypatch.setenv("LOG_POSITIONS", "debug")
    path = tmp_path / "navtracker.log"
    try:
        configure_logging(str(path))
        log_position(
            TrackerPosition(
                tracker_id=9,
                latitude=1.0,
                longitude=2.0,
                timestamp=datetime(2026, 1, 1, tzinfo=UTC),
            ),
            source="websocket",
            compared_to=None,
            normalized=datetime(2026, 1, 1, tzinfo=UTC),
            decision="publish",
        )
    finally:
        _reset(previous, level, positions_level)
    assert "tracker_id=9" in path.read_text(encoding="utf-8")
