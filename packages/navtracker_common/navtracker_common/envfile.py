"""Load a ``.env`` file without overriding variables already set.

Docker Compose injects ``.env`` itself. ``python -m analytics.main`` and
``python -m app.main`` call this so the same file works in a local
terminal on Windows or Linux. Existing environment variables win.
"""

import os
from pathlib import Path


def load_env_file(path: Path | None = None) -> None:
    """Set missing environment keys from ``path``. A missing file is ignored."""

    file = path or Path(".env")
    if not file.is_file():
        return
    for raw in file.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.lower().startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        name = key.strip()
        if not name or name in os.environ:
            continue
        os.environ[name] = _unquote(value.strip())


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        return value[1:-1]
    return value
