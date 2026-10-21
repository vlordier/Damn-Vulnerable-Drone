#!/usr/bin/env python3
"""Join one replay result with independent controller and Gazebo evidence."""

import argparse
import json
from pathlib import Path

from build_evidence_manifest import build, write_manifest
from evidence_contracts import (
    exact_count,
    ordered_timestamps,
    read_json_object,
    run_identity_matches,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("run_dir", type=Path)
    args = parser.parse_args()
    replay = read_json_object(args.run_dir / "research-waypoint-replay.json")
    proof_path = args.run_dir / "gz-flight-proof-report.json"
    proof = read_json_object(proof_path) if proof_path.is_file() else None
    measured = proof.get("measured", {}) if proof else {}
    grant = (
        proof.get("localGrant", {}) if proof and isinstance(proof.get("localGrant"), dict) else {}
    )
    run_identity_ok = bool(
        proof and run_identity_matches(args.run_dir, replay.get("runId"), grant.get("runId"))
    )
    event_order_ok = ordered_timestamps(
        replay.get("captureAtUnixMs"),
        replay.get("replayPublishedAtUnixMs"),
        replay.get("rejectionObservedAtUnixMs"),
    )
    flight_gates_passed = bool(
        proof
        and proof.get("localFlightPassed") is True
        and proof.get("poseTimelinePassed") is True
        and proof.get("actuatorNeutralizationPassed") is True
        and measured.get("allLegsReached") is True
    )
    replay_qualified = (
        replay.get("antiReplayRejectionObserved") is True
        and flight_gates_passed
        and run_identity_ok
        and exact_count(replay, "publishedMessages", 1)
        and event_order_ok
    )
    result = {
        "contract": "damn-vulnerable-drone.antoine-replay-result",
        "version": "1.0.0",
        "scenario": replay["scenario"],
        "runId": grant.get("runId") if proof else None,
        "runIdentityMatches": run_identity_ok,
        "exactlyOneReplayPublished": exact_count(replay, "publishedMessages", 1),
        "eventOrderValid": event_order_ok,
        "replay": replay,
        "controllerStopReasons": proof.get("stopReasons") if proof else None,
        "independentGazebo": {
            "localFlightPassed": proof.get("localFlightPassed") if proof else None,
            "poseTimelinePassed": proof.get("poseTimelinePassed") if proof else None,
            "actuatorNeutralizationPassed": proof.get("actuatorNeutralizationPassed")
            if proof
            else None,
            "allLegsReached": measured.get("allLegsReached"),
            "peakAltitudeM": measured.get("peakAltitudeM"),
            "maxHorizontalTravelM": measured.get("maxHorizontalTravelM"),
        },
        "replayQualified": replay_qualified,
        "interpretation": (
            "One same-run replay of a valid setpoint was published without credentials and rejected "
            "by the controller's vehicle/sequence check. The captured run continued to pass its "
            "independent flight proof. This covers only an exact duplicate while the original run "
            "is live; it does not establish protection against all stale, cross-run, or reordered cases."
            if replay_qualified
            else "The replay or its flight outcome was inconclusive; inspect the raw evidence before drawing a security conclusion."
        ),
    }
    output = args.run_dir / "research-waypoint-replay-summary.json"
    output.write_text(json.dumps(result, indent=2) + "\n")
    manifest = build(args.run_dir)
    write_manifest(args.run_dir, manifest)
    print(output)
    return 0 if replay_qualified else 2


if __name__ == "__main__":
    raise SystemExit(main())
