#!/usr/bin/env python3
"""Sign one bounded GPS test envelope for a confirmed run-owned loopback broker."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from urllib.parse import urlsplit

from dvd_research.run_nats import read_run_nats_log
from dvd_research.sensor_fixture import build_case


def verify_scope(server: str, run_dir: Path) -> None:
    parsed = urlsplit(server)
    if (
        parsed.scheme != "nats"
        or parsed.hostname != "127.0.0.1"
        or parsed.port is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("fixture signer accepts only an explicit IPv4 loopback NATS URL")
    if run_dir.is_symlink() or not run_dir.is_dir() or not run_dir.name.startswith("antoine-gps-"):
        raise ValueError("fixture signer requires an existing run-local GPS evidence directory")
    content = read_run_nats_log(run_dir)
    if (
        content is None
        or f"Listening for client connections on 127.0.0.1:{parsed.port}" not in content
        or "Server is ready" not in content
    ):
        raise ValueError("requested broker is not confirmed by this run's NATS log")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--server", required=True)
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument(
        "--case",
        required=True,
        choices=[
            "fresh-sample",
            "duplicate-sequence",
            "stale-sample",
            "future-sample",
            "out-of-order-sequence",
        ],
    )
    parser.add_argument("--now-ms", required=True, type=int)
    args = parser.parse_args()
    try:
        verify_scope(args.server, args.run_dir)
        source = json.load(sys.stdin)
        if not isinstance(source, dict):
            raise ValueError("fixture input must be one JSON object on stdin")
        signed = build_case(source, args.case, args.now_ms)
        print(json.dumps(signed, sort_keys=True, separators=(",", ":"), allow_nan=False))
        return 0
    except (OSError, json.JSONDecodeError, ValueError) as error:
        print(f"fixture refused: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
