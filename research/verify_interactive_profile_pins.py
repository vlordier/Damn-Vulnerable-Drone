#!/usr/bin/env python3
"""Compare an interactive profile receipt with an explicit reviewed pin file."""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

PIN_CONTRACT = "damn-vulnerable-drone.interactive-profile-pins"
PIN_VERSION = "1.0.0"
DIRTY_SOURCE_FIELDS = (
    "worktreeStatusSha256",
    "trackedDiffSha256",
    "dirtyFilesSha256",
)
SOURCE_SNAPSHOT_FOR = {"antoine": "antoine", "gazeboAssets": "gazebo"}
SNAPSHOT_FIELDS = ("path", "receiptSha256", "snapshotManifestSha256", "fileCount")
HEADLESS_PROFILE = "headless-antoine-gazebo-mavlink"
HEADLESS_OPTIONAL_ARTIFACTS = {"qgroundcontrolBinarySha256", "qgcSettingsSha256"}


def verify(receipt: Any, pins: Any) -> dict[str, Any]:
    """Classify a receipt against manually reviewed exact source and build pins."""
    missing: list[str] = []
    incompatible: list[str] = []
    dirty_unreviewed: list[str] = []

    if not isinstance(receipt, dict) or receipt.get("contract") != (
        "damn-vulnerable-drone.interactive-profile"
    ):
        missing.append("receipt contract is missing or unsupported")
    if not isinstance(pins, dict) or pins.get("contract") != PIN_CONTRACT:
        missing.append("pin contract is missing or unsupported")
    if missing:
        return _result("missing", missing, incompatible, dirty_unreviewed)

    if pins.get("version") != PIN_VERSION:
        missing.append("pin version is missing or unsupported")
    _validate_review(pins.get("review"), missing)
    if receipt.get("version") != "1.0.0":
        missing.append("receipt version is missing or unsupported")
    source_snapshots_match = _compare_source_snapshots(
        receipt.get("sourceSnapshots"),
        pins.get("sourceSnapshots"),
        missing,
        incompatible,
    )
    profile = receipt.get("profile")
    expected_profile = pins.get("profile")
    if not isinstance(profile, str) or not profile:
        missing.append("receipt profile is missing")
    elif not isinstance(expected_profile, str) or not expected_profile:
        missing.append("pinned profile is missing")
    elif profile != expected_profile:
        incompatible.append("profile differs from reviewed pin")

    _compare_mapping(
        "source",
        receipt.get("source"),
        pins.get("source"),
        missing,
        incompatible,
        dirty_unreviewed,
        check_dirty=True,
        source_snapshots_match=source_snapshots_match,
        source_snapshots=receipt.get("sourceSnapshots"),
    )
    _compare_mapping(
        "artifacts",
        receipt.get("artifacts"),
        pins.get("artifacts"),
        missing,
        incompatible,
        dirty_unreviewed,
        allow_absent_qgc=profile == HEADLESS_PROFILE,
    )
    _compare_mapping(
        "toolchain",
        receipt.get("toolchain"),
        pins.get("toolchain"),
        missing,
        incompatible,
        dirty_unreviewed,
    )

    if missing:
        status = "missing"
    elif incompatible:
        status = "incompatible"
    elif dirty_unreviewed:
        status = "dirty-unreviewed"
    else:
        status = "verified"
    return _result(status, missing, incompatible, dirty_unreviewed)


def _compare_source_snapshots(
    actual: Any,
    expected: Any,
    missing: list[str],
    incompatible: list[str],
) -> bool:
    if actual is None and expected is None:
        return False
    if not isinstance(actual, dict) or not actual:
        missing.append("receipt source snapshots are missing")
        return False
    if not isinstance(expected, dict) or not expected:
        missing.append("reviewed source snapshot pins are missing")
        return False
    if set(actual) != set(expected):
        missing.append("source snapshot pin names do not match receipt")
        return False
    matches = True
    for name, actual_record in actual.items():
        expected_record = expected.get(name)
        if not isinstance(actual_record, dict) or not isinstance(expected_record, dict):
            missing.append(f"sourceSnapshots.{name} record is malformed")
            matches = False
            continue
        for field in SNAPSHOT_FIELDS:
            actual_value = actual_record.get(field)
            expected_value = expected_record.get(field)
            if actual_value is None or expected_value is None:
                missing.append(f"sourceSnapshots.{name}.{field} is missing")
                matches = False
            elif actual_value != expected_value:
                incompatible.append(f"sourceSnapshots.{name}.{field} differs from reviewed pin")
    return matches


def _compare_mapping(
    label: str,
    actual: Any,
    expected: Any,
    missing: list[str],
    incompatible: list[str],
    dirty_unreviewed: list[str],
    *,
    check_dirty: bool = False,
    source_snapshots_match: bool = False,
    source_snapshots: Any = None,
    allow_absent_qgc: bool = False,
) -> None:
    if not isinstance(actual, dict) or not actual:
        missing.append(f"receipt {label} are missing")
        return
    if not isinstance(expected, dict) or not expected:
        missing.append(f"reviewed {label} pins are missing")
        return
    actual_values = dict(actual)
    expected_values = dict(expected)
    if label == "artifacts" and allow_absent_qgc:
        for name in HEADLESS_OPTIONAL_ARTIFACTS:
            if actual_values.get(name) is None:
                actual_values.pop(name, None)
            if expected_values.get(name) is None:
                expected_values.pop(name, None)
    if set(actual_values) != set(expected_values):
        missing.append(f"{label} pin names do not match receipt")
        return

    if label != "source":
        for name, actual_value in actual_values.items():
            expected_value = expected_values.get(name)
            if actual_value is None or expected_value is None:
                missing.append(f"{label}.{name} is missing")
            elif not isinstance(actual_value, str) or not isinstance(expected_value, str):
                missing.append(f"{label}.{name} must be a string")
            elif actual_value != expected_value:
                incompatible.append(f"{label}.{name} differs from reviewed pin")
        return

    for name, actual_record in actual.items():
        expected_record = expected.get(name)
        if not isinstance(actual_record, dict) or not isinstance(expected_record, dict):
            missing.append(f"{label}.{name} record is malformed")
            continue
        if check_dirty and actual_record.get("classification") == "unavailable":
            snapshot_name = SOURCE_SNAPSHOT_FOR.get(name)
            snapshots = source_snapshots if isinstance(source_snapshots, dict) else {}
            if (
                not source_snapshots_match
                or snapshot_name is None
                or snapshot_name not in snapshots
            ):
                missing.append(
                    f"source.{name} is unavailable and lacks a matching reviewed source snapshot"
                )
                continue
            # The original checkout metadata is unavailable, but this profile's
            # complete run-owned source bundle is content-pinned and verified.
            for field, actual_value in actual_record.items():
                expected_value = expected_record.get(field)
                if actual_value is None:
                    if expected_value is not None:
                        incompatible.append(f"source.{name}.{field} differs from reviewed pin")
                elif expected_value is None:
                    missing.append(f"source.{name}.{field} is missing from reviewed pin")
                elif actual_value != expected_value:
                    incompatible.append(f"source.{name}.{field} differs from reviewed pin")
            continue
        if check_dirty and actual_record.get("classification") not in {
            "clean",
            "dirty-unreviewed",
        }:
            missing.append(f"source.{name} classification is missing or unavailable")
        fields = (
            set(actual_record)
            if label != "source"
            else {
                "revision",
                "classification",
                *DIRTY_SOURCE_FIELDS,
                "dirtyFileCount",
            }
        )
        if check_dirty and actual_record.get("classification") == "dirty-unreviewed":
            unpinned = [
                field
                for field in DIRTY_SOURCE_FIELDS
                if not isinstance(expected_record.get(field), str) or not expected_record[field]
            ]
            if unpinned:
                dirty_unreviewed.append(
                    f"source.{name} dirty state lacks reviewed pins: {', '.join(unpinned)}"
                )
        for field in sorted(fields):
            actual_value = actual_record.get(field)
            expected_value = expected_record.get(field)
            if (
                check_dirty
                and actual_record.get("classification") == "dirty-unreviewed"
                and field in DIRTY_SOURCE_FIELDS
                and actual_value is not None
                and expected_value is None
            ):
                continue
            if actual_value is None or expected_value is None:
                missing.append(f"{label}.{name}.{field} is missing")
            elif actual_value != expected_value:
                incompatible.append(f"{label}.{name}.{field} differs from reviewed pin")


def _validate_review(review: Any, missing: list[str]) -> None:
    if not isinstance(review, dict) or review.get("status") != "reviewed":
        missing.append("pins are not explicitly marked reviewed")
        return
    reviewer = review.get("reviewer")
    reviewed_at = review.get("reviewedAtUtc")
    if not isinstance(reviewer, str) or not reviewer.strip():
        missing.append("pin reviewer is missing")
    if not isinstance(reviewed_at, str):
        missing.append("pin review timestamp is missing")
        return
    try:
        parsed = datetime.fromisoformat(reviewed_at.replace("Z", "+00:00"))
    except ValueError:
        missing.append("pin review timestamp is invalid")
        return
    if parsed.tzinfo is None or parsed.utcoffset() != UTC.utcoffset(parsed):
        missing.append("pin review timestamp must be UTC")


def _result(
    status: str,
    missing: list[str],
    incompatible: list[str],
    dirty_unreviewed: list[str],
) -> dict[str, Any]:
    return {
        "contract": "damn-vulnerable-drone.interactive-profile-pin-check",
        "version": "1.0.0",
        "status": status,
        "verified": status == "verified",
        "missing": missing,
        "incompatible": incompatible,
        "dirtyUnreviewed": dirty_unreviewed,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--pins", type=Path, required=True)
    args = parser.parse_args()
    try:
        receipt = json.loads(args.receipt.read_text(encoding="utf-8"))
        pins = json.loads(args.pins.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        parser.error(str(error))
    result = verify(receipt, pins)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["verified"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
