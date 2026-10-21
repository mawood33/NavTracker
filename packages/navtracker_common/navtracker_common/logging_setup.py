"""Stdout logging plus an optional rotating file.

``LOG_FILE`` empty disables the file. Unset uses the service default.
``LOG_LEVEL``, ``LOG_MAX_BYTES``, and ``LOG_BACKUP_COUNT`` apply to both
the worker and analytics.
"""

import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from navtracker_common.settings import bool_setting

DEFAULT_LEVEL = "INFO"
DEFAULT_MAX_BYTES = 10 * 1024 * 1024
DEFAULT_BACKUP_COUNT = 5
POSITIONS_LOGGER = "navtracker.positions"
_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"


def configure_logging(default_file: str) -> None:
    """Attach stdout and, unless ``LOG_FILE`` is blank, a rotating file."""

    level = _level(os.getenv("LOG_LEVEL", DEFAULT_LEVEL))
    _record_level, positions_level = position_logging()
    max_bytes = _non_negative("LOG_MAX_BYTES", os.getenv("LOG_MAX_BYTES"), DEFAULT_MAX_BYTES)
    backup_count = _non_negative(
        "LOG_BACKUP_COUNT",
        os.getenv("LOG_BACKUP_COUNT"),
        DEFAULT_BACKUP_COUNT,
    )
    raw_file = os.getenv("LOG_FILE")
    path = default_file if raw_file is None else raw_file.strip()

    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()
    root.setLevel(level)
    formatter = logging.Formatter(_FORMAT)
    stdout = logging.StreamHandler(sys.stdout)
    stdout.setFormatter(formatter)
    root.addHandler(stdout)
    positions = logging.getLogger(POSITIONS_LOGGER)
    positions.setLevel(logging.NOTSET if positions_level is None else positions_level)
    if not path:
        return
    destination = Path(path)
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise RuntimeError(
            f"Cannot create log directory for LOG_FILE={path!r}: {exc}"
        ) from exc
    rotating = RotatingFileHandler(
        destination,
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
    )
    rotating.setFormatter(formatter)
    root.addHandler(rotating)


def position_logging() -> tuple[int, int | None]:
    """Return ``(record_level, logger_level)`` for tracker positions.

    Unset ``LOG_POSITIONS`` logs at INFO. ``true`` does the same. ``false``
    logs at DEBUG and leaves the logger unset, so the lines stay quiet
    until ``navtracker.positions`` is raised to DEBUG. ``debug`` raises
    that logger to DEBUG without lowering the root level.
    """

    raw = os.getenv("LOG_POSITIONS")
    if raw is None:
        return logging.INFO, logging.INFO
    if raw.strip().lower() == "debug":
        return logging.DEBUG, logging.DEBUG
    if bool_setting("LOG_POSITIONS", raw):
        return logging.INFO, logging.INFO
    return logging.DEBUG, None


def _level(value: str | None) -> int:
    text = (value or "").strip() or DEFAULT_LEVEL
    level = logging.getLevelName(text.upper())
    if isinstance(level, int):
        return level
    raise RuntimeError(
        f"Invalid LOG_LEVEL={text!r}. Expected a logging level name such as INFO."
    )


def _non_negative(name: str, value: str | None, default: int) -> int:
    text = default if value is None or value.strip() == "" else value.strip()
    try:
        number = int(text)
    except (TypeError, ValueError):
        raise RuntimeError(
            f"Invalid {name}={value!r}. Expected a non-negative integer."
        ) from None
    if number < 0:
        raise RuntimeError(
            f"Invalid {name}={value!r}. Expected a non-negative integer."
        )
    return number
