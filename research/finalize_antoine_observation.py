#!/usr/bin/env python3
"""Attach the enclosing flight-run result to a completed NATS capture."""

import argparse
import json
from pathlib import Path

from build_evidence_manifest import build, write_manifest
from evidence_contracts import read_json_object, run_identity_matches


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--flight-exit", required=True, type=int)
    args = parser.parse_args()
    path = args.run_dir / "research-nats-observation.json"
    report = read_json_object(path)
    proof_path = args.run_dir / "gz-flight-proof-report.json"
    proof = read_json_object(proof_path) if proof_path.is_file() else None
    identity_matches = bool(
        proof
        and run_identity_matches(
            args.run_dir,
            report.get("runId"),
            (proof.get("localGrant") or {}).get("runId")
            if isinstance(proof.get("localGrant"), dict)
            else None,
        )
    )
    report["flightProof"] = {
        "exitCode": args.flight_exit,
        "reportAvailable": proof is not None,
        "localFlightPassed": proof.get("localFlightPassed") if proof else None,
        "runIdentityMatches": identity_matches,
    }
    if (
        not identity_matches
        or args.flight_exit != 0
        or proof is None
        or not proof.get("localFlightPassed")
    ):
        report["qualificationNote"] = (
            "NATS message visibility was captured from the run-owned local authority and state "
            "publishers. The enclosing flight proof did not pass; this artifact makes no claim "
            "about vehicle motion or successful end-to-end flight."
        )
    path.write_text(json.dumps(report, indent=2) + "\n")
    manifest = build(args.run_dir)
    write_manifest(args.run_dir, manifest)
    print(path)
    return 0 if identity_matches else 2


if __name__ == "__main__":
    raise SystemExit(main())
