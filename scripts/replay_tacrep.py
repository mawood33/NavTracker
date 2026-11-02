#!/usr/bin/env python3
"""POST TACREP text, JSON, or CSV to a local analytics server.

    python scripts/replay_tacrep.py --file fixtures/dev/tacrep_track.txt --token dev

``--token`` is the analytics write token. ``--no-analyze`` skips the
development-only analysis pass.
"""

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "packages" / "navtracker_common"))

from analytics.infrastructure.replay_files import read_tacrep_file  # noqa: E402
from analytics.infrastructure.replay_http import post_json, post_text  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Replay TACREP files into analytics")
    parser.add_argument("--url", default="http://127.0.0.1:8080")
    parser.add_argument("--token", default="")
    parser.add_argument("--file", action="append", required=True, dest="files")
    parser.add_argument("--source", default="dev-replay")
    parser.add_argument("--analyze", action=argparse.BooleanOptionalAction, default=True)
    args = parser.parse_args(argv)
    accepted = 0
    for name in args.files:
        body = read_tacrep_file(Path(name))
        status, payload = post_text(
            args.url.rstrip("/") + "/v1/events/tacrep",
            body,
            token=args.token,
            source=args.source,
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
