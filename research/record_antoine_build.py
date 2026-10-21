#!/usr/bin/env python3
"""Write a digest receipt tying the Antoine SITL source and patch to a binary."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def git_revision(source: Path) -> str | None:
    root = source.parent
    for parent in source.parents:
        if (parent / ".git").exists():
            root = parent
            break
    try:
        return subprocess.check_output(
            ["git", "-C", str(root), "rev-parse", "HEAD"], text=True
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def artifact(path: Path) -> dict[str, str]:
    if not path.is_file():
        raise SystemExit(f"required build input is missing: {path}")
    return {"name": path.name, "sha256": sha256(path)}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--patch", type=Path, required=True)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--additional-binary", type=Path, action="append", default=[])
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--toolchain", required=True)
    args = parser.parse_args()

    source = artifact(args.source)
    source["gitRevision"] = git_revision(args.source) or "unknown"
    receipt = {
        "schema": "dvd.antoine-build-receipt.v1",
        "createdAtUtc": datetime.now(UTC).isoformat(),
        "toolchain": args.toolchain,
        "source": source,
        "patch": artifact(args.patch),
        "binary": artifact(args.binary),
        "additionalBinaries": [artifact(path) for path in args.additional_binary],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(receipt, indent=2))


if __name__ == "__main__":
    main()
