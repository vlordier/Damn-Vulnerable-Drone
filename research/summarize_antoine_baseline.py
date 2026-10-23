#!/usr/bin/env python3
"""Normalize the authorized Antoine flight proof as a reusable baseline."""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

from build_evidence_manifest import build, write_manifest
from evidence_contracts import read_json_object, run_identity_matches


def build_baseline_result(run_dir: Path, proof: dict, launcher_exit: int) -> dict:
    """Build eligibility from independent flight, stop, identity, and launcher gates."""
    measured = proof.get("measured", {}) if isinstance(proof.get("measured"), dict) else {}
    grant = proof.get("localGrant") if isinstance(proof.get("localGrant"), dict) else {}
    run_id = grant.get("runId")
    ground_dwell = measured.get("groundSettleDwellSecondsObserved")
    ground_dwell_valid = type(ground_dwell) is int or (
        type(ground_dwell) is float and math.isfinite(ground_dwell)
    )
    gates = {
        "launcherExitZero": launcher_exit == 0,
        "runIdentityMatches": run_identity_matches(run_dir, run_id),
        "localFlightPassed": proof.get("localFlightPassed") is True,
        "allLegsReached": measured.get("allLegsReached") is True,
        "poseTimelinePassed": proof.get("poseTimelinePassed") is True,
        "actuatorNeutralizationPassed": proof.get("actuatorNeutralizationPassed") is True,
        # Upstream deliberately leaves physicalSafeStopPassed null: pose plus
        # motor neutralization is not a physical safety claim. This gate only
        # qualifies an authorized flight control with measured ground dwell.
        "groundSettlePassed": (
            measured.get("settledAtGround") is True and ground_dwell_valid and ground_dwell >= 1.0
        ),
    }
    return {
        "contract": "damn-vulnerable-drone.antoine-baseline-result",
        "version": "1.0.0",
        "scenario": "authorized-baseline-antoine",
        "runId": run_id,
        "scope": "authorized local test-authority flight; run-owned loopback NATS; Gazebo X500",
        "authorityScope": proof.get("authorityScope"),
        "physicsEngine": None,
        "gates": gates,
        "baselineEligible": all(gates.values()),
        "runIdentityMatches": gates["runIdentityMatches"],
        "measurements": {
            "allLegsReached": measured.get("allLegsReached"),
            "peakAltitudeM": measured.get("peakAltitudeM"),
            "horizontalTravelM": measured.get("maxHorizontalTravelM"),
            "poseSamples": measured.get("poseSamples"),
            "actuatorCommands": measured.get("signedActuatorCommands"),
            "neutralRows": measured.get("applierNeutralRows"),
            "settledAtGround": measured.get("settledAtGround"),
            "groundSettleDwellSecondsObserved": measured.get("groundSettleDwellSecondsObserved"),
        },
        "interpretation": (
            "Authorized baseline passed flight, pose, actuator, measured ground-settle, and launcher gates; suitable as a flight comparison candidate."
            if all(gates.values())
            else "Baseline is incomplete or failed one or more gates; do not use it as a successful control."
        ),
        "evidenceFiles": [
            "gz-flight-proof-report.json",
            "flight-evidence.json",
            "gazebo-pose.jsonl",
            "physics-engine.txt",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--launcher-exit", type=int, required=True)
    args = parser.parse_args()
    run_dir = args.run_dir
    proof_path = run_dir / "gz-flight-proof-report.json"
    proof = read_json_object(proof_path)
    result = build_baseline_result(run_dir, proof, args.launcher_exit)
    if (run_dir / "physics-engine.txt").is_file():
        result["physicsEngine"] = (
            (run_dir / "physics-engine.txt").read_text(encoding="utf-8").strip()
        )
    output = run_dir / "research-baseline-summary.json"
    output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    manifest = build(run_dir)
    write_manifest(run_dir, manifest)
    print(output)
    return 0 if result["baselineEligible"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
