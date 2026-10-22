#!/usr/bin/env python3
"""Apply the canonical QGC patch without touching a modified checkout."""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path


def apply_patch_safely(root: Path, patch: Path) -> str:
    """Return applied/already-applied, or fail before modifying unsafe inputs."""
    root = root.resolve(strict=True)
    patch = patch.resolve(strict=True)
    git_root = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        check=False,
    )
    if git_root.returncode or Path(git_root.stdout.strip()).resolve() != root:
        raise RuntimeError(f"QGroundControl root is not a Git checkout root: {root}")

    reverse = subprocess.run(
        ["git", "-C", str(root), "apply", "--reverse", "--check", str(patch)],
        capture_output=True,
        text=True,
        check=False,
    )
    if reverse.returncode == 0:
        return "already-applied"

    dirty = subprocess.run(
        ["git", "-C", str(root), "diff", "--quiet", "HEAD", "--"],
        capture_output=True,
        text=True,
        check=False,
    )
    if dirty.returncode == 1:
        raise RuntimeError(
            "refusing to apply the QGroundControl patch to a checkout with tracked changes"
        )
    if dirty.returncode:
        details = dirty.stderr.strip() or dirty.stdout.strip()
        raise RuntimeError(f"could not inspect QGroundControl tracked changes: {details}")

    applicable = subprocess.run(
        ["git", "-C", str(root), "apply", "--check", str(patch)],
        capture_output=True,
        text=True,
        check=False,
    )
    if applicable.returncode:
        details = applicable.stderr.strip() or applicable.stdout.strip()
        raise RuntimeError(f"QGroundControl patch does not apply cleanly: {details}")

    applied = subprocess.run(
        ["git", "-C", str(root), "apply", str(patch)],
        capture_output=True,
        text=True,
        check=False,
    )
    if applied.returncode:
        details = applied.stderr.strip() or applied.stdout.strip()
        raise RuntimeError(f"could not apply QGroundControl patch: {details}")
    return "applied"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True, help="QGroundControl Git checkout")
    parser.add_argument("--patch", type=Path, required=True, help="canonical Antoine patch")
    args = parser.parse_args()
    try:
        result = apply_patch_safely(args.root, args.patch)
    except (OSError, RuntimeError) as error:
        parser.error(str(error))
    print(f"QGroundControl patch {result}: {args.patch}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
