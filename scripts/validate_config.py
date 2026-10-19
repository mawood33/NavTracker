#!/usr/bin/env python3
"""Validate startup for development, staging, and production.

Run from the repository root:

    python scripts/validate_config.py
    python scripts/validate_config.py development
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config_validation import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
