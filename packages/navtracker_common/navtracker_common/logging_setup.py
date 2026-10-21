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

DEFAULT_LEVEL = "INFO"
DEFAULT_MAX_BYTES = 10 * 1024 * 1024
DEFAULT_BACKUP_COUNT = 5
_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"


def configure_logging(default_file: str) -> None:
    """Attach stdout and, unless ``LOG_FILE`` is blank, a rotating file."""

    level = _level(os.getenv("LOG_LEVEL", DEFAULT_LEVEL))
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
