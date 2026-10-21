#!/usr/bin/env python3
"""Remove Cargo intermediates from a successfully accepted Antoine rebuild."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

from record_antoine_run_build import BINARIES, sha256


def read_object(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"required regular receipt is missing: {path}")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"receipt must contain a JSON object: {path}")
    return value


def compact(rebuild_dir: Path) -> dict[str, Any]:
    rebuild_dir = rebuild_dir.resolve(strict=True)
    cargo_target = rebuild_dir / "cargo-target"
    if cargo_target.is_symlink() or not cargo_target.is_dir():
        raise ValueError("rebuild cargo target must be a regular directory")

    rebuild_receipt_path = rebuild_dir / "rebuild-receipt.json"
    run_build_path = rebuild_dir / "antoine-run-build-receipt.json"
    native_receipt_path = rebuild_dir / "source-snapshot/antoine-source-build-receipt.json"
    acceptance_path = rebuild_dir / "acceptance-run/gazebo-mavlink-acceptance.json"
    provenance_path = rebuild_dir / "acceptance-run/research-provenance-comparison.json"
    stop_path = rebuild_dir / "acceptance-run/interactive-profile-stop-result.json"
    rebuild_receipt = read_object(rebuild_receipt_path)
    run_build = read_object(run_build_path)
    acceptance = read_object(acceptance_path)
    provenance = read_object(provenance_path)
    stop_result = read_object(stop_path)

    if rebuild_receipt.get("contract") != "damn-vulnerable-drone.antoine-snapshot-rebuild":
        raise ValueError("unsupported rebuild receipt")
    if rebuild_receipt.get("nativeBuildReceiptSha256") != sha256(native_receipt_path):
        raise ValueError("native build receipt differs from the rebuild receipt")
    if rebuild_receipt.get("runBuildReceiptSha256") != sha256(run_build_path):
        raise ValueError("run build receipt differs from the rebuild receipt")
    if run_build.get("contract") != "damn-vulnerable-drone.antoine-run-build":
        raise ValueError("unsupported Antoine run build receipt")
    if run_build.get("nativeBuildReceiptSha256") != sha256(native_receipt_path):
        raise ValueError("run build receipt does not bind the native build receipt")
    if (
        acceptance.get("schema") != "dvd.gazebo-mavlink-flight-check.v1"
        or acceptance.get("passed") is not True
        or acceptance.get("finalMavlinkState", {}).get("armed") is not False
    ):
        raise ValueError("compaction requires passing, finally disarmed Gazebo acceptance")
    if provenance.get("stable") is not True:
        raise ValueError("compaction requires stable source provenance")
    if stop_result.get("status") != "stopped":
        raise ValueError("compaction requires a completed identity-checked profile stop")
    stop_checks = stop_result.get("results")
    if not isinstance(stop_checks, dict) or any(
        value
        not in {
            "stopped",
            "already-stopped",
            "already-stopped-stale-marker-cleaned",
            "closed",
            "preserved-user-owned",
        }
        for value in stop_checks.values()
    ):
        raise ValueError("compaction requires all run-owned processes stopped and ports closed")

    native = read_object(native_receipt_path)
    build = native.get("build")
    if not isinstance(build, dict):
        raise ValueError("native build receipt has no build record")
    support = build.get("support_artifacts")
    if not isinstance(support, dict):
        raise ValueError("native build receipt has no support artifact records")
    native_hashes = {
        BINARIES[0]: build.get("artifact_sha256"),
        BINARIES[1]: support.get("antoine_gz_local_authority", {}).get("artifact_sha256"),
        BINARIES[2]: support.get("antoine-boundary-service", {}).get("artifact_sha256"),
    }
    artifact_records = run_build.get("artifacts")
    if not isinstance(artifact_records, list):
        raise ValueError("run build receipt has no artifact list")
    artifact_hashes = {
        Path(item["path"]).name: item.get("sha256")
        for item in artifact_records
        if isinstance(item, dict) and isinstance(item.get("path"), str)
    }
    if artifact_hashes != native_hashes:
        raise ValueError("run and native receipts do not describe the same three binaries")

    snapshot_target = (
        rebuild_dir / "source-snapshot/antoine/.cargo-target/antoine-sitl-build-receipt"
    )
    if not snapshot_target.is_symlink() or snapshot_target.resolve(strict=True) != cargo_target:
        raise ValueError("sealed source snapshot does not link to this rebuild cargo target")
    binary_dir = cargo_target / "debug"
    if binary_dir.is_symlink() or not binary_dir.is_dir():
        raise ValueError("Cargo debug artifact directory is missing or unsafe")
    copied_dir = rebuild_dir / "acceptance-run/flight-proof/antoine-binaries"
    copied_hashes: dict[str, str] = {}
    staged_files: list[tuple[Path, str]] = []
    for name in BINARIES:
        source = binary_dir / name
        copied = copied_dir / name
        expected_hash = native_hashes[name]
        if (
            source.is_symlink()
            or not source.is_file()
            or not source.stat().st_mode & 0o111
            or sha256(source) != expected_hash
        ):
            raise ValueError(f"rebuilt binary is missing or does not match receipt: {source}")
        if copied.is_symlink() or not copied.is_file() or sha256(copied) != expected_hash:
            raise ValueError(f"accepted run binary is missing or does not match receipt: {copied}")
        copied_hashes[name] = expected_hash
        staged_files.append((source, name))

    stage = Path(tempfile.mkdtemp(prefix=".cargo-target-compact-", dir=rebuild_dir))
    try:
        staged_debug = stage / "debug"
        staged_debug.mkdir()
        for source, name in staged_files:
            shutil.copy2(source, staged_debug / name)
        for _, name in staged_files:
            if sha256(staged_debug / name) != native_hashes[name]:
                raise ValueError(f"staged binary hash mismatch: {name}")

        backup = rebuild_dir / ".cargo-target-before-compaction"
        if backup.exists() or backup.is_symlink():
            raise ValueError("refusing to overwrite existing compaction backup")
        os.replace(cargo_target, backup)
        try:
            os.replace(stage, cargo_target)
        except BaseException:
            os.replace(backup, cargo_target)
            raise
        shutil.rmtree(backup)
    finally:
        if stage.exists():
            shutil.rmtree(stage)

    return {
        "status": "compacted",
        "retainedBinaries": copied_hashes,
        "removedCargoIntermediates": True,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("rebuild_dir", type=Path)
    args = parser.parse_args()
    try:
        result = compact(args.rebuild_dir)
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as error:
        print(f"Antoine rebuild target compaction failed: {error}", file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
