#!/usr/bin/env python3
"""Validate ingest and analytics startup for one or more environments.

Run from the repository root:

    python scripts/validate_config.py
    python scripts/validate_config.py development
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "packages" / "navtracker_common"))

from app.config_validation import main as ingest_main  # noqa: E402

from analytics.config_validation import main as analytics_main  # noqa: E402


def main(argv: list[str]) -> int:
    ingest = ingest_main(argv)
    analytics = analytics_main(argv)
    if ingest == 0 and analytics == 0:
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
