#!/usr/bin/env python3
"""Verify an Antoine native receipt using the build's recorded Rust flags."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

from build_antoine_source_snapshot import add_stable_rust_path_remaps


def verify_native_receipt(root: Path, snapshot_root: Path, receipt_path: Path) -> dict[str, str]:
    """Run Antoine's verifier with the same path remaps used for the sealed build."""
    root = root.resolve(strict=True)
    snapshot_root = snapshot_root.absolute()
    receipt_path = receipt_path.absolute()
    if receipt_path.is_symlink() or not receipt_path.is_file():
        raise ValueError("Antoine native build receipt must be a regular file")
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    expected_flags_hash = (
        receipt.get("toolchain", {})
        .get("build_environment_sha256", {})
        .get("CARGO_ENCODED_RUSTFLAGS")
    )
    environment = os.environ.copy()
    if expected_flags_hash:
        add_stable_rust_path_remaps(environment, snapshot_root)
        actual_flags_hash = hashlib.sha256(
            environment["CARGO_ENCODED_RUSTFLAGS"].encode()
        ).hexdigest()
        if actual_flags_hash != expected_flags_hash:
            raise ValueError("cannot reconstruct Rust flags recorded in Antoine native receipt")
    receipt_tool = root / "scripts/antoine_sitl_build_receipt.py"
    environment["PYTHONPATH"] = str(root)
    subprocess.run(
        [
            sys.executable,
            str(receipt_tool),
            "verify",
            "--root",
            str(root),
            "--receipt",
            str(receipt_path),
        ],
        cwd=root,
        env=environment,
        check=True,
    )
    return environment


def write_shell_environment(path: Path, environment: dict[str, str]) -> None:
    """Write a private, non-overwriting shell fragment for verified build flags."""
    lines = []
    for name in ("CARGO_ENCODED_RUSTFLAGS", "PYTHONPATH"):
        value = environment.get(name)
        if value is None:
            lines.append(f"unset {name}")
        else:
            lines.append(f"export {name}={shlex.quote(value)}")
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write("\n".join(lines) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--snapshot-root", type=Path, required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--write-shell-environment", type=Path)
    args = parser.parse_args()
    try:
        environment = verify_native_receipt(args.root, args.snapshot_root, args.receipt)
        if args.write_shell_environment:
            write_shell_environment(args.write_shell_environment, environment)
    except (OSError, ValueError, json.JSONDecodeError, subprocess.SubprocessError) as error:
        print(f"Antoine native receipt verification failed: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
