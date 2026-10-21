#!/usr/bin/env python3
"""Compare an interactive profile receipt with an explicit reviewed pin file."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

PIN_CONTRACT = "damn-vulnerable-drone.interactive-profile-pins"
PIN_VERSION = "1.0.0"
DIRTY_SOURCE_FIELDS = (
    "worktreeStatusSha256",
    "trackedDiffSha256",
    "dirtyFilesSha256",
)


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
    if receipt.get("version") != "1.0.0":
        missing.append("receipt version is missing or unsupported")
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
    )
    _compare_mapping(
        "artifacts",
        receipt.get("artifacts"),
        pins.get("artifacts"),
        missing,
        incompatible,
        dirty_unreviewed,
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


def _compare_mapping(
    label: str,
    actual: Any,
    expected: Any,
    missing: list[str],
    incompatible: list[str],
    dirty_unreviewed: list[str],
    *,
    check_dirty: bool = False,
) -> None:
    if not isinstance(actual, dict) or not actual:
        missing.append(f"receipt {label} are missing")
        return
    if not isinstance(expected, dict) or not expected:
        missing.append(f"reviewed {label} pins are missing")
        return
    if set(actual) != set(expected):
        missing.append(f"{label} pin names do not match receipt")
        return

    if label != "source":
        for name, actual_value in actual.items():
            expected_value = expected.get(name)
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
