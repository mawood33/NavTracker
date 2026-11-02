#!/usr/bin/env python3
"""Start analytics with the repo and navtracker_common on sys.path.

Works from the repository root on Windows and Linux:

    python scripts/run_analytics.py
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "packages" / "navtracker_common"))

from analytics.main import main  # noqa: E402

if __name__ == "__main__":
    main()
