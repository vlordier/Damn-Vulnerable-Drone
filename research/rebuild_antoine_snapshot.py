#!/usr/bin/env python3
"""Rebuild Antoine binaries from a verified profile run's source snapshot."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from create_interactive_profile_receipt import snapshot_record
from gazebo_version import gazebo_version
from record_antoine_run_build import sha256
from seal_antoine_source_snapshot import archive, seal


def command_output(*command: str) -> str:
    result = subprocess.run(command, check=True, capture_output=True, text=True)
    return (result.stdout or result.stderr).strip()


def verify_profile_inputs(run_dir: Path) -> dict[str, Any]:
    run_dir = run_dir.resolve(strict=True)
    receipt_path = run_dir / "interactive-profile-receipt.json"
    if receipt_path.is_symlink() or not receipt_path.is_file():
        raise ValueError("profile run is missing its regular interactive profile receipt")
    profile = json.loads(receipt_path.read_text(encoding="utf-8"))
    if (
        profile.get("contract") != "damn-vulnerable-drone.interactive-profile"
        or profile.get("runId") != run_dir.name
        or profile.get("status") not in {"ready", "stopped"}
    ):
        raise ValueError("profile receipt identity or status is not rebuildable")
    expected = profile.get("sourceSnapshots", {}).get("antoine")
    if not isinstance(expected, dict) or expected.get("path") != "antoine-source-snapshot":
        raise ValueError("profile receipt does not bind an Antoine source snapshot")
    actual = snapshot_record(
        run_dir,
        run_dir / "antoine-source-snapshot",
        "antoine-source-snapshot.json",
        "damn-vulnerable-drone.antoine-source-snapshot",
    )
    if (
        actual.get("receiptSha256") != expected.get("receiptSha256")
        or actual.get("snapshotManifestSha256") != expected.get("snapshotManifestSha256")
        or actual.get("fileCount") != expected.get("fileCount")
    ):
        raise ValueError("Antoine source snapshot differs from the profile receipt")
    acceptance_path = run_dir / "gazebo-mavlink-acceptance.json"
    comparison_path = run_dir / "research-provenance-comparison.json"
    if any(path.is_symlink() or not path.is_file() for path in (acceptance_path, comparison_path)):
        raise ValueError("profile run is missing its acceptance or provenance comparison report")
    acceptance = json.loads(acceptance_path.read_text(encoding="utf-8"))
    final_state = acceptance.get("finalMavlinkState")
    if (
        acceptance.get("schema") != "dvd.gazebo-mavlink-flight-check.v1"
        or acceptance.get("passed") is not True
        or not isinstance(final_state, dict)
        or final_state.get("armed") is not False
        or acceptance.get("prearmReadyBeforeFlight") is not True
    ):
        raise ValueError("profile run lacks a passing headless acceptance with final disarm")
    comparison = json.loads(comparison_path.read_text(encoding="utf-8"))
    if comparison.get("runId") != run_dir.name or comparison.get("stable") is not True:
        raise ValueError("profile run does not have stable matching pre/post provenance")
    toolchain = profile.get("toolchain")
    if not isinstance(toolchain, dict):
        raise ValueError("profile receipt is missing its toolchain")
    gz_bin = Path(os.environ.get("GZ_BIN", Path.home() / "Work/gz-macOS/ws/install/bin/gz"))
    actual_toolchain = {
        "cargo": command_output("cargo", "--version"),
        "rustc": command_output("rustc", "--version"),
        "gazebo": gazebo_version(gz_bin),
    }
    if any(toolchain.get(key) != actual_toolchain[key] for key in actual_toolchain):
        raise ValueError("installed Cargo/Rust/Gazebo versions differ from the profile receipt")
    return {"profile": profile, "snapshot": actual, "toolchain": actual_toolchain}


def rebuild(run_dir: Path, output_dir: Path, rustup_toolchain: str) -> dict[str, Any]:
    run_dir = run_dir.resolve(strict=True)
    if output_dir.exists() or output_dir.is_symlink():
        raise ValueError(f"refusing to overwrite rebuild output: {output_dir}")
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    inputs = verify_profile_inputs(run_dir)
    if output_dir.resolve().is_relative_to(run_dir):
        raise ValueError("rebuild output must be outside the source evidence run")

    source_snapshot = output_dir / "source-snapshot"
    cargo_target = output_dir / "cargo-target"
    cargo_target.mkdir(parents=True)
    archive(run_dir / "antoine-source-snapshot", source_snapshot)
    seal(source_snapshot, cargo_target)

    antoine_root = source_snapshot / "antoine"
    native_receipt = source_snapshot / "antoine-source-build-receipt.json"
    environment = os.environ.copy()
    environment.update(
        CARGO_NET_OFFLINE="true",
        PYTHONPATH=str(antoine_root),
        RUSTUP_TOOLCHAIN=rustup_toolchain,
    )
    receipt_tool = antoine_root / "scripts/antoine_sitl_build_receipt.py"
    for operation in ("build", "verify"):
        subprocess.run(
            [
                "uv",
                "run",
                "--no-project",
                "--script",
                str(receipt_tool),
                operation,
                "--root",
                str(antoine_root),
                "--receipt",
                str(native_receipt),
            ],
            cwd=antoine_root,
            env=environment,
            check=True,
        )

    source_revision = command_output("git", "-C", str(antoine_root), "rev-parse", "HEAD")
    binary_dir = cargo_target / "debug"
    run_build_receipt = output_dir / "antoine-run-build-receipt.json"
    subprocess.run(
        [
            "uv",
            "run",
            "--project",
            str(Path(__file__).resolve().parent),
            "python",
            str(Path(__file__).with_name("record_antoine_run_build.py")),
            "--antoine-root",
            str(antoine_root),
            "--binary-dir",
            str(binary_dir),
            "--run-id",
            f"rebuild-{inputs['profile']['runId']}",
            "--source-revision",
            source_revision,
            "--native-build-receipt",
            str(native_receipt),
            "--output",
            str(run_build_receipt),
        ],
        cwd=run_dir,
        env=environment,
        check=True,
    )
    summary = {
        "contract": "damn-vulnerable-drone.antoine-snapshot-rebuild",
        "version": "1.0.0",
        "sourceRunId": inputs["profile"]["runId"],
        "sourceSnapshotManifestSha256": inputs["snapshot"]["snapshotManifestSha256"],
        "toolchain": inputs["toolchain"],
        "nativeBuildReceiptSha256": sha256(native_receipt),
        "runBuildReceiptSha256": sha256(run_build_receipt),
        "binaryDirectory": "source-snapshot/antoine/.cargo-target/antoine-sitl-build-receipt/debug",
        "acceptanceCommand": "make antoine-gz-rebuild-check",
    }
    summary_path = output_dir / "rebuild-receipt.json"
    summary_path.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--rustup-toolchain", default="1.99.1")
    args = parser.parse_args()
    try:
        result = rebuild(args.run_dir, args.output_dir, args.rustup_toolchain)
    except (
        OSError,
        ValueError,
        KeyError,
        TypeError,
        json.JSONDecodeError,
        subprocess.SubprocessError,
    ) as error:
        print(f"Antoine snapshot rebuild failed: {error}", file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
