#!/usr/bin/env python3
"""Verify a run-local Antoine evidence manifest without trusting its claims."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from dvd_research.evidence_contract import verify_manifest as verify
from dvd_research.evidence_contract import verify_signature


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path, help="run directory or manifest JSON path")
    parser.add_argument(
        "--trusted-public-key",
        type=Path,
        default=Path(os.environ["DVD_RESEARCH_TRUSTED_PUBLIC_KEY"])
        if os.environ.get("DVD_RESEARCH_TRUSTED_PUBLIC_KEY")
        else None,
    )
    args = parser.parse_args()
    path = (
        args.manifest / "research-evidence-manifest.json"
        if args.manifest.is_dir()
        else args.manifest
    )
    valid, errors = verify(path)
    signature_status, signature_errors = verify_signature(path, args.trusted_public_key)
    print(
        json.dumps(
            {
                "integrityCheck": "passed" if valid else "failed",
                "manifest": str(path),
                "integrityErrors": errors,
                "signatureStatus": signature_status,
                "signatureErrors": signature_errors,
                "trustLimit": "Manifest signatures authenticate possession of a local key only when its public key is pinned; they do not attest to which binaries the operating system loaded.",
            },
            indent=2,
        )
    )
    signature_ok = signature_status in {
        "unsigned",
        "valid_trusted_signature",
        "valid_unpinned_signature",
    }
    return 0 if valid and signature_ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
