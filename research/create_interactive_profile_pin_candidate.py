#!/usr/bin/env python3
"""Create an unreviewed pin candidate from a stable interactive-profile run."""

from __future__ import annotations

import argparse
import json
import math
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from create_interactive_profile_receipt import snapshot_record

PIN_CONTRACT = "damn-vulnerable-drone.interactive-profile-pins"
COMPARISON_CONTRACT = "damn-vulnerable-drone.antoine-provenance-comparison"
DIRTY_SOURCE_FIELDS = (
    "worktreeStatusSha256",
    "trackedDiffSha256",
    "dirtyFilesSha256",
)
SOURCE_SNAPSHOT_FOR = {"antoine": "antoine", "gazeboAssets": "gazebo"}
HEADLESS_PROFILE = "headless-antoine-gazebo-mavlink"
HEADLESS_OPTIONAL_ARTIFACTS = {"qgroundcontrolBinarySha256", "qgcSettingsSha256"}
SNAPSHOT_INPUTS = {
    "antoine": (
        "antoine-source-snapshot",
        "antoine-source-snapshot.json",
        "damn-vulnerable-drone.antoine-source-snapshot",
    ),
    "gazebo": (
        "gazebo-source-snapshot",
        "gazebo-source-snapshot.json",
        "damn-vulnerable-drone.gazebo-source-snapshot",
    ),
}


def _validate_source_snapshots(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or not value:
        raise ValueError("receipt source snapshots are missing")
    for name, record in value.items():
        if (
            not isinstance(record, dict)
            or not isinstance(record.get("path"), str)
            or not record["path"]
            or not isinstance(record.get("receiptSha256"), str)
            or not record["receiptSha256"]
            or not isinstance(record.get("snapshotManifestSha256"), str)
            or not record["snapshotManifestSha256"]
            or type(record.get("fileCount")) is not int
            or record["fileCount"] <= 0
        ):
            raise ValueError(f"receipt source snapshot is incomplete: {name}")
    return value


def validate_run_evidence(receipt: dict[str, Any], run_directory: Path) -> None:
    """Rehash the run-owned snapshots and require successful headless acceptance."""
    run_directory = run_directory.resolve(strict=True)
    if receipt.get("runId") != run_directory.name:
        raise ValueError("receipt run ID does not match its evidence directory")
    recorded_directory = receipt.get("runDirectory")
    if (
        not isinstance(recorded_directory, str)
        or Path(recorded_directory).resolve() != run_directory
    ):
        raise ValueError("receipt run directory does not match its evidence directory")

    snapshots = receipt.get("sourceSnapshots")
    if snapshots is not None:
        _validate_source_snapshots(snapshots)
        if set(snapshots) != set(SNAPSHOT_INPUTS):
            raise ValueError("receipt must bind both Antoine and Gazebo source snapshots")
        for name, (expected_path, filename, contract) in SNAPSHOT_INPUTS.items():
            record = snapshots[name]
            if record.get("path") != expected_path:
                raise ValueError(f"receipt source snapshot path is invalid: {name}")
            actual = snapshot_record(
                run_directory,
                run_directory / expected_path,
                filename,
                contract,
            )
            if actual != record:
                raise ValueError(f"run-owned source snapshot differs from its receipt: {name}")

    if receipt.get("profile") == HEADLESS_PROFILE:
        report_path = run_directory / "gazebo-mavlink-acceptance.json"
        if report_path.is_symlink() or not report_path.is_file():
            raise ValueError("headless pin candidate requires a regular acceptance report")
        report = json.loads(report_path.read_text(encoding="utf-8"))
        if not isinstance(report, dict):
            raise ValueError("headless acceptance report must be an object")
        final_state = report.get("finalMavlinkState")
        final_pose = report.get("finalGazeboPose")
        if (
            report.get("schema") != "dvd.gazebo-mavlink-flight-check.v1"
            or report.get("passed") is not True
            or report.get("validGazeboTelemetryReceived") is not True
            or report.get("callsign") != receipt.get("callsign")
            or not isinstance(final_state, dict)
            or final_state.get("armed") is not False
            or not isinstance(final_pose, list)
            or len(final_pose) != 3
            or any(
                not isinstance(value, (int, float))
                or isinstance(value, bool)
                or not math.isfinite(value)
                for value in final_pose
            )
        ):
            raise ValueError("headless pin candidate requires passing disarmed Gazebo acceptance")


def create_candidate(receipt: Any, comparison: Any) -> dict[str, Any]:
    """Copy reviewed-input fields only from a complete, stable profile run."""
    if (
        not isinstance(receipt, dict)
        or receipt.get("contract") != "damn-vulnerable-drone.interactive-profile"
        or receipt.get("version") != "1.0.0"
    ):
        raise ValueError("unsupported or incomplete interactive profile receipt")
    if not isinstance(comparison, dict) or comparison.get("contract") != COMPARISON_CONTRACT:
        raise ValueError("unsupported provenance comparison")
    if comparison.get("version") != "1.0.0" or comparison.get("stable") is not True:
        raise ValueError("pin candidates require a stable provenance comparison")
    run_id = receipt.get("runId")
    if not isinstance(run_id, str) or not run_id or comparison.get("runId") != run_id:
        raise ValueError("receipt and provenance comparison run IDs differ")
    required_true = (
        "prePhaseValid",
        "postPhaseValid",
        "profileMatches",
        "toolchainMatches",
        "patchMatches",
    )
    if any(comparison.get(field) is not True for field in required_true):
        raise ValueError("provenance comparison has incomplete acceptance gates")
    if comparison.get("changedSourceTrees") != [] or comparison.get("changedInputs") != []:
        raise ValueError("provenance comparison reports changed inputs")

    profile = receipt.get("profile")
    source = receipt.get("source")
    source_snapshots = receipt.get("sourceSnapshots")
    artifacts = receipt.get("artifacts")
    toolchain = receipt.get("toolchain")
    if not isinstance(profile, str) or not profile:
        raise ValueError("receipt profile is missing")
    if not isinstance(source, dict) or not source:
        raise ValueError("receipt source records are missing")
    if not isinstance(artifacts, dict) or not artifacts:
        raise ValueError("receipt artifact records are missing")
    if not isinstance(toolchain, dict) or not toolchain:
        raise ValueError("receipt toolchain records are missing")
    if source_snapshots is not None:
        source_snapshots = _validate_source_snapshots(source_snapshots)

    pinned_artifacts = dict(artifacts)
    if profile == HEADLESS_PROFILE:
        for name in HEADLESS_OPTIONAL_ARTIFACTS:
            if pinned_artifacts.get(name) is None:
                pinned_artifacts.pop(name, None)

    for name, record in source.items():
        if not isinstance(record, dict):
            raise ValueError(f"receipt source record is incomplete: {name}")
        classification = record.get("classification")
        if classification == "unavailable":
            snapshot_name = SOURCE_SNAPSHOT_FOR.get(name)
            if snapshot_name is None or not isinstance(source_snapshots, dict):
                raise ValueError(f"receipt source state is unavailable: {name}")
            _validate_source_snapshots(source_snapshots)
            if snapshot_name not in source_snapshots:
                raise ValueError(f"receipt source snapshot is missing: {snapshot_name}")
            continue
        if classification not in {"clean", "dirty-unreviewed"} or not isinstance(
            record.get("revision"), str
        ):
            raise ValueError(f"receipt source state is unavailable: {name}")
        if any(
            not isinstance(record.get(field), str) or not record[field]
            for field in DIRTY_SOURCE_FIELDS
        ):
            raise ValueError(f"source digests are incomplete: {name}")
        if not isinstance(record.get("dirtyFileCount"), int):
            raise ValueError(f"source dirty-file count is missing: {name}")
    for section_name, values in (("artifacts", pinned_artifacts), ("toolchain", toolchain)):
        if any(not isinstance(value, str) or not value for value in values.values()):
            raise ValueError(f"receipt {section_name} contains unavailable values")

    return {
        "contract": PIN_CONTRACT,
        "version": "1.0.0",
        "profile": profile,
        "source": source,
        **({"sourceSnapshots": source_snapshots} if source_snapshots is not None else {}),
        "artifacts": pinned_artifacts,
        "toolchain": toolchain,
        "review": {
            "status": "unreviewed",
            "generatedFromRunId": run_id,
            "generatedAtUtc": datetime.now(UTC).isoformat(),
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--comparison", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        receipt = json.loads(args.receipt.read_text(encoding="utf-8"))
        comparison = json.loads(args.comparison.read_text(encoding="utf-8"))
        validate_run_evidence(receipt, args.receipt.parent)
        candidate = create_candidate(receipt, comparison)
        payload = (json.dumps(candidate, indent=2, sort_keys=True) + "\n").encode()
        fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as output:
            output.write(payload)
    except (OSError, json.JSONDecodeError, ValueError) as error:
        parser.error(str(error))
    print(f"Wrote unreviewed pin candidate from run {candidate['review']['generatedFromRunId']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
