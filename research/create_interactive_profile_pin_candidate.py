#!/usr/bin/env python3
"""Create an unreviewed pin candidate from a stable interactive-profile run."""

from __future__ import annotations

import argparse
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

PIN_CONTRACT = "damn-vulnerable-drone.interactive-profile-pins"
COMPARISON_CONTRACT = "damn-vulnerable-drone.antoine-provenance-comparison"
DIRTY_SOURCE_FIELDS = (
    "worktreeStatusSha256",
    "trackedDiffSha256",
    "dirtyFilesSha256",
)


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

    for name, record in source.items():
        if not isinstance(record, dict) or not isinstance(record.get("revision"), str):
            raise ValueError(f"receipt source record is incomplete: {name}")
        classification = record.get("classification")
        if classification not in {"clean", "dirty-unreviewed"}:
            raise ValueError(f"receipt source state is unavailable: {name}")
        if any(
            not isinstance(record.get(field), str) or not record[field]
            for field in DIRTY_SOURCE_FIELDS
        ):
            raise ValueError(f"source digests are incomplete: {name}")
        if not isinstance(record.get("dirtyFileCount"), int):
            raise ValueError(f"source dirty-file count is missing: {name}")
    for section_name, values in (("artifacts", artifacts), ("toolchain", toolchain)):
        if any(not isinstance(value, str) or not value for value in values.values()):
            raise ValueError(f"receipt {section_name} contains unavailable values")

    return {
        "contract": PIN_CONTRACT,
        "version": "1.0.0",
        "profile": profile,
        "source": source,
        "artifacts": artifacts,
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
