#!/usr/bin/env python3
"""Seal and build the exact Antoine source bundle captured for a profile run."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from create_interactive_profile_receipt import snapshot_record
from seal_antoine_source_snapshot import seal

SNAPSHOT_CONTRACT = "damn-vulnerable-drone.antoine-source-snapshot"
NATIVE_RECEIPT_NAME = "antoine-source-build-receipt.json"
BINARY_RELATIVE = Path(".cargo-target/antoine-sitl-build-receipt/debug")
BINARIES = (
    "antoine_sitl_nats",
    "antoine_gz_local_authority",
    "antoine-boundary-service",
)


def add_stable_rust_path_remaps(environment: dict[str, str], source_root: Path) -> None:
    """Normalize embedded source/cache paths while preserving caller Rust flags."""
    encoded_flags = environment.get("CARGO_ENCODED_RUSTFLAGS")
    flags = (
        encoded_flags.split("\x1f") if encoded_flags else environment.get("RUSTFLAGS", "").split()
    )
    cargo_home = Path(environment.get("CARGO_HOME", Path.home() / ".cargo")).resolve()
    flags.extend(
        (
            f"--remap-path-prefix={source_root.resolve(strict=True)}=.",
            f"--remap-path-prefix={cargo_home}=/cargo",
        )
    )
    environment["CARGO_ENCODED_RUSTFLAGS"] = "\x1f".join(flags)


def _run(command: list[str], *, cwd: Path, environment: dict[str, str]) -> None:
    subprocess.run(command, cwd=cwd, env=environment, check=True)


def build_snapshot(snapshot_dir: Path, target_dir: Path, rustup_toolchain: str) -> dict[str, Any]:
    """Rehash, seal, test, build, and verify one run-owned Antoine snapshot."""
    snapshot_dir = snapshot_dir.resolve(strict=True)
    run_dir = snapshot_dir.parent.resolve(strict=True)
    before = snapshot_record(
        run_dir,
        snapshot_dir,
        "antoine-source-snapshot.json",
        SNAPSHOT_CONTRACT,
    )
    if target_dir.exists() or target_dir.is_symlink():
        raise ValueError(f"refusing to overwrite Antoine snapshot build target: {target_dir}")
    target_dir.parent.mkdir(parents=True, exist_ok=True)
    target_dir.mkdir()

    sealed = seal(snapshot_dir, target_dir)
    root = snapshot_dir / "antoine"
    native_receipt = snapshot_dir / NATIVE_RECEIPT_NAME
    receipt_tool = root / "scripts/antoine_sitl_build_receipt.py"
    if not receipt_tool.is_file():
        raise ValueError("Antoine snapshot does not include its native receipt tool")

    environment = os.environ.copy()
    environment.update(
        CARGO_TARGET_DIR=str(root / ".cargo-target/antoine-sitl-build-receipt"),
        CARGO_NET_OFFLINE="true",
        PYTHONPATH=str(root),
        RUSTUP_TOOLCHAIN=rustup_toolchain,
    )
    add_stable_rust_path_remaps(environment, snapshot_dir)
    manifest = root / "sitl/antoine-sitl/Cargo.toml"
    _run(
        [
            "cargo",
            "test",
            "--locked",
            "--manifest-path",
            str(manifest),
            "--features",
            "nats",
            "--bin",
            "antoine_sitl_nats",
            "gazebo_plant_honors_configured_battery_drain_rate",
        ],
        cwd=root,
        environment=environment,
    )
    for operation in ("build", "verify"):
        _run(
            [
                "uv",
                "run",
                "--no-project",
                "--script",
                str(receipt_tool),
                operation,
                "--root",
                str(root),
                "--receipt",
                str(native_receipt),
            ],
            cwd=root,
            environment=environment,
        )

    binary_dir = root / BINARY_RELATIVE
    missing = [
        name
        for name in BINARIES
        if not (binary_dir / name).is_file() or not os.access(binary_dir / name, os.X_OK)
    ]
    if missing:
        raise ValueError("Antoine snapshot build omitted binaries: " + ", ".join(missing))
    after = snapshot_record(
        run_dir,
        snapshot_dir,
        "antoine-source-snapshot.json",
        SNAPSHOT_CONTRACT,
    )
    if after != before:
        raise ValueError("Antoine source snapshot changed during its sealed build")
    return {
        "contract": "damn-vulnerable-drone.antoine-source-snapshot-build",
        "version": "1.0.0",
        "sourceSnapshot": before,
        "sealedCommits": sealed["commits"],
        "buildRoot": str(root),
        "nativeReceipt": str(native_receipt),
        "binaryDirectory": str(binary_dir),
        "binaries": {name: str(binary_dir / name) for name in BINARIES},
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot-dir", type=Path, required=True)
    parser.add_argument("--target-dir", type=Path, required=True)
    parser.add_argument("--rustup-toolchain", default="1.99.1")
    args = parser.parse_args()
    try:
        result = build_snapshot(args.snapshot_dir, args.target_dir, args.rustup_toolchain)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print(f"Antoine sealed source build failed: {error}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
