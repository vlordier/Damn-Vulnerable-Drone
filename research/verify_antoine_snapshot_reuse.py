#!/usr/bin/env python3
"""Verify a prebuilt Antoine bundle exactly matches a profile's captured source."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from create_interactive_profile_receipt import snapshot_record

SNAPSHOT_CONTRACT = "damn-vulnerable-drone.antoine-source-snapshot"
SNAPSHOT_RECEIPT = "antoine-source-snapshot.json"
MATCH_FIELDS = ("receiptSha256", "snapshotManifestSha256", "fileCount")


def verify_reuse(current_snapshot: Path, prebuilt_snapshot: Path) -> dict[str, Any]:
    """Fail unless the run-owned capture and sealed prebuilt bundle are identical."""
    current_snapshot = current_snapshot.absolute()
    prebuilt_snapshot = prebuilt_snapshot.absolute()
    current = snapshot_record(
        current_snapshot.parent,
        current_snapshot,
        SNAPSHOT_RECEIPT,
        SNAPSHOT_CONTRACT,
    )
    prebuilt = snapshot_record(
        prebuilt_snapshot.parent,
        prebuilt_snapshot,
        SNAPSHOT_RECEIPT,
        SNAPSHOT_CONTRACT,
    )
    differences = [field for field in MATCH_FIELDS if current[field] != prebuilt[field]]
    if differences:
        raise ValueError(
            "prebuilt Antoine source snapshot differs from this run ("
            + ", ".join(differences)
            + ")"
        )
    return {
        "contract": "damn-vulnerable-drone.antoine-snapshot-reuse-verification",
        "version": "1.0.0",
        "status": "verified",
        "snapshotManifestSha256": current["snapshotManifestSha256"],
        "fileCount": current["fileCount"],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("current_snapshot", type=Path)
    parser.add_argument("prebuilt_snapshot", type=Path)
    args = parser.parse_args()
    try:
        result = verify_reuse(args.current_snapshot, args.prebuilt_snapshot)
    except (OSError, ValueError, KeyError) as error:
        print(f"Antoine snapshot reuse verification failed: {error}", file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
