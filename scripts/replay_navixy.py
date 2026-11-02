#!/usr/bin/env python3
"""POST a Navixy JSON replay file to the development analytics endpoint.

    python scripts/replay_navixy.py --file fixtures/dev/navixy_halt.json --token dev

The server must be running with APP_ENV=development. Outside development
the route is not found.
"""

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "packages" / "navtracker_common"))

from analytics.infrastructure.replay_files import read_navixy_file  # noqa: E402
from analytics.infrastructure.replay_http import post_json  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Replay Navixy JSON into analytics")
    parser.add_argument("--url", default="http://127.0.0.1:8080")
    parser.add_argument("--token", default="")
    parser.add_argument("--file", action="append", required=True, dest="files")
    parser.add_argument("--analyze", action=argparse.BooleanOptionalAction, default=True)
    args = parser.parse_args(argv)
    accepted = 0
    for name in args.files:
        fixes = read_navixy_file(Path(name))
        status, payload = post_json(
            args.url.rstrip("/") + "/v1/dev/fixes",
            {"fixes": list(fixes)},
            token=args.token,
        )
        print(f"{name} status={status} {payload}")
        if status != 200:
            return 1
        count = payload.get("accepted")
        if isinstance(count, int):
            accepted += count
    print(f"accepted={accepted}")
    if args.analyze:
        status, payload = post_json(args.url.rstrip("/") + "/v1/dev/analyze", {}, token=args.token)
        print(f"analyze status={status} {payload}")
        if status != 200:
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
