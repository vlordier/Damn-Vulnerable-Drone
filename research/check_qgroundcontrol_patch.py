#!/usr/bin/env python3
"""Verify that the configured QGroundControl checkout contains our full patch."""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path


def verify_applied_patch(root: Path, patch: Path) -> None:
    result = subprocess.run(
        ["git", "-C", str(root), "apply", "--reverse", "--check", str(patch)],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        details = result.stderr.strip() or result.stdout.strip()
        message = f"{patch} is not fully applied to the QGroundControl checkout at {root}"
        if details:
            message = f"{message}: {details}"
        raise RuntimeError(message)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True, help="QGroundControl Git checkout")
    parser.add_argument("--patch", type=Path, required=True, help="canonical Antoine patch")
    args = parser.parse_args()
    try:
        verify_applied_patch(args.root, args.patch)
    except (OSError, RuntimeError) as error:
        parser.error(str(error))
    print(f"Verified canonical Antoine patch in {args.root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
