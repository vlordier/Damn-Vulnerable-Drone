#!/usr/bin/env python3
"""Verify that the configured QGroundControl checkout contains our full patch."""

from __future__ import annotations

import argparse
import os
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


def verify_binary_in_checkout(root: Path, binary: Path) -> None:
    checkout = root.resolve(strict=True)
    executable = binary.resolve(strict=True)
    try:
        executable.relative_to(checkout)
    except ValueError as error:
        raise RuntimeError(
            f"QGroundControl executable {executable} is outside its source checkout {checkout}"
        ) from error
    if not executable.is_file() or not os.access(executable, os.X_OK):
        raise RuntimeError(f"QGroundControl executable is not executable: {executable}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True, help="QGroundControl Git checkout")
    parser.add_argument("--patch", type=Path, required=True, help="canonical Antoine patch")
    parser.add_argument("--binary", type=Path, required=True, help="executable to launch")
    args = parser.parse_args()
    try:
        verify_binary_in_checkout(args.root, args.binary)
        verify_applied_patch(args.root, args.patch)
    except (OSError, RuntimeError) as error:
        parser.error(str(error))
    print(f"Verified QGroundControl executable and canonical Antoine patch in {args.root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
