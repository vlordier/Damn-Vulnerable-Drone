#!/usr/bin/env python3
"""Join attack, controller, applier, and independent Gazebo evidence."""

from __future__ import annotations

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
    parser.add_argument("--baseline", type=Path)
    args = parser.parse_args()
    run_dir = args.run_dir
    attack = read_json_object(run_dir / "research-waypoint-injection.json")
    flight = read_json_object(run_dir / "gz-flight-proof-report.json")
    baseline = read_json_object(args.baseline) if args.baseline else None

    measured = flight.get("measured", {})
    measured = measured if isinstance(measured, dict) else {}
    flight_grant = flight.get("localGrant", {})
    flight_grant = flight_grant if isinstance(flight_grant, dict) else {}
    original = attack.get("originalPositionEnuM")
    mutated = attack.get("mutatedPositionEnuM")
    delta = None
    if (
        isinstance(original, list)
        and isinstance(mutated, list)
        and len(original) == 3
        and len(mutated) == 3
        and all(type(value) in (int, float) for value in original + mutated)
    ):
        delta = [round(float(b) - float(a), 6) for a, b in zip(original, mutated, strict=False)]

    run_id = flight_grant.get("runId")
    attack_run_id_matches = run_identity_matches(
        run_dir, run_id, attack.get("runId"), attack.get("expectedRunId")
    )
    stop = attack.get("postAttackNeutralStop")
    stop = stop if isinstance(stop, dict) else {}
    publish_at = attack.get("attackPublishStartedAtUnixMs")
    accept_at = attack.get("parserAcceptanceObservedAtUnixMs")
    stop_at = stop.get("recordedAtUnixMs")
    causal_order = bool(
        attack_run_id_matches
        and exact_count(attack, "publishedMessages", 1)
        and attack.get("setpointParserAcceptedMutation") is True
        and attack.get("neutralStopMatchedRun") is True
        and attack.get("neutralActuatorOutputObserved") is True
        and stop.get("runId") == run_id
        and stop.get("contract") == "furia.gzsitl.actuator-output-audit"
        and stop.get("stage") == "explicit-stop"
        and stop.get("actuatorKind") == "motors"
        and stop.get("vehicleId") == "drone-1"
        and stop.get("authority") == "safe-stop"
        and stop.get("neutral") is True
        and ordered_timestamps(publish_at, accept_at, stop_at)
    )
    attack_pose_passed = flight.get("poseTimelinePassed") is True
    attack_actuator_passed = flight.get("actuatorNeutralizationPassed") is True
    attack_all_legs_reached = measured.get("allLegsReached") is True
    stop_reasons = flight.get("stopReasons", {})
    stop_reasons = stop_reasons if isinstance(stop_reasons, dict) else {}
    mismatch_stop_observed = stop_reasons.get("mission-setpoint-missing-stale-or-mismatched", 0) > 0
    attack_impact_qualified = (
        causal_order
        and mismatch_stop_observed
        and attack_pose_passed
        and attack_actuator_passed
        and not attack_all_legs_reached
    )
    result = {
        "contract": "damn-vulnerable-drone.antoine-research-result",
        "version": "1.0.0",
        "scenario": "waypoint-injection-antoine",
        "runId": run_id,
        "scope": attack.get("scope"),
        "authorityScope": flight.get("authorityScope"),
        "qualification": flight.get("qualification"),
        "physicsEngine": (run_dir / "physics-engine.txt").read_text(encoding="utf-8").strip()
        if (run_dir / "physics-engine.txt").is_file()
        else None,
        "attack": {
            "publishedMessages": attack.get("publishedMessages"),
            "targetSubject": attack.get("targetSubject"),
            "capturedSequence": attack.get("capturedSequence"),
            "injectedSequence": attack.get("injectedSequence"),
            "originalPositionEnuM": original,
            "mutatedPositionEnuM": mutated,
            "positionDeltaM": delta,
            "setpointParserAcceptedMutation": attack.get("setpointParserAcceptedMutation"),
            "setpointEnvelopeHadSignatureField": attack.get("capturedSetpointHadSignatureField"),
            "attackPublishStartedAtUnixMs": attack.get("attackPublishStartedAtUnixMs"),
            "parserAcceptanceObservedAtUnixMs": attack.get("parserAcceptanceObservedAtUnixMs"),
            "postAttackNeutralStop": attack.get("postAttackNeutralStop"),
            "causalOrderObserved": causal_order,
            "impactQualified": attack_impact_qualified,
            "grantMismatchStopObserved": mismatch_stop_observed,
            "runIdentityMatches": attack_run_id_matches,
            "publishToStopMs": attack.get("publishToStopMs"),
        },
        "controller": {
            "stopReasons": flight.get("stopReasons", {}),
            "grant": flight.get("localGrant", {}),
            "vigieAdmission": flight.get("vigieAdmission", {}),
        },
        "applier": {
            "signedActuatorCommands": measured.get("signedActuatorCommands"),
            "stopCommands": measured.get("stopCommands"),
            "neutralRows": measured.get("applierNeutralRows"),
            "neutralActuatorOutputObserved": attack.get("neutralActuatorOutputObserved"),
            "actuatorNeutralizationPassed": flight.get("actuatorNeutralizationPassed"),
        },
        "independentGazebo": {
            "truthSource": flight.get("truthSource"),
            "poseTimelinePassed": flight.get("poseTimelinePassed"),
            "localFlightPassed": flight.get("localFlightPassed"),
            "allLegsReached": measured.get("allLegsReached"),
            "startEnuM": measured.get("startEnuM"),
            "endEnuM": measured.get("endEnuM"),
            "peakAltitudeM": measured.get("peakAltitudeM"),
            "maxHorizontalTravelM": measured.get("maxHorizontalTravelM"),
            "poseSamples": measured.get("poseSamples"),
        },
        "baseline": None,
        "comparison": None,
        "interpretation": (
            "The local setpoint publisher reached the typed parser. The changed target did not "
            "match the locally signed grant target; the controller emitted a stop and the applier "
            "observed neutral output. Independent Gazebo evidence shows no authorized waypoint "
            "flight in this run. This demonstrates denial of takeoff in this local demo integration, "
            "not target takeover or a production/Core vulnerability."
            if attack_impact_qualified
            else "Attack/parser/neutral evidence is present, but post-publication stop ordering was "
            "not established; treat the causal result as inconclusive."
        ),
        "evidenceFiles": [
            "research-waypoint-injection.json",
            "gz-flight-proof-report.json",
            "flight-evidence.json",
            "applier-neutral/actuator-output.jsonl",
            "gazebo-pose.jsonl",
        ],
    }
    if baseline is not None:
        baseline_grant = baseline.get("localGrant", {})
        baseline_grant = baseline_grant if isinstance(baseline_grant, dict) else {}
        is_summary = baseline.get("contract") == "damn-vulnerable-drone.antoine-baseline-result"
        bm = baseline.get("measurements", {}) if is_summary else baseline.get("measured", {})
        bm = bm if isinstance(bm, dict) else {}
        baseline_gates = baseline.get("gates", {})
        baseline_gates = baseline_gates if isinstance(baseline_gates, dict) else {}
        baseline_physics_path = args.baseline.parent / "physics-engine.txt"
        baseline_physics = baseline.get("physicsEngine")
        if not baseline_physics and baseline_physics_path.is_file():
            baseline_physics = baseline_physics_path.read_text().strip()
        baseline_run_id = baseline.get("runId") or baseline_grant.get("runId")
        baseline_authority = baseline.get("authorityScope")
        local_flight_passed = (
            baseline_gates.get("localFlightPassed")
            if is_summary
            else baseline.get("localFlightPassed")
        )
        all_legs_reached = bm.get("allLegsReached")
        baseline_pose_passed = (
            baseline_gates.get("poseTimelinePassed")
            if is_summary
            else baseline.get("poseTimelinePassed")
        )
        baseline_actuator_passed = (
            baseline_gates.get("actuatorNeutralizationPassed")
            if is_summary
            else baseline.get("actuatorNeutralizationPassed")
        )
        physics_matches = bool(baseline_physics and baseline_physics == result["physicsEngine"])
        authority_matches = bool(
            baseline_authority and baseline_authority == result["authorityScope"]
        )
        flight_passed = local_flight_passed is True and all_legs_reached is True
        baseline_eligible = (
            baseline.get("baselineEligible") is True
            and baseline_gates.get("poseTimelinePassed") is True
            and baseline_gates.get("actuatorNeutralizationPassed") is True
            if is_summary
            else flight_passed and baseline_pose_passed is True and baseline_actuator_passed is True
        )
        comparable = physics_matches and authority_matches and flight_passed and baseline_eligible
        comparable = comparable and attack_pose_passed and attack_actuator_passed
        result["baseline"] = {
            "runId": baseline_run_id,
            "authorityScope": baseline_authority,
            "physicsEngine": baseline_physics,
            "localFlightPassed": local_flight_passed,
            "allLegsReached": all_legs_reached,
            "peakAltitudeM": bm.get("peakAltitudeM"),
            "maxHorizontalTravelM": bm.get("maxHorizontalTravelM", bm.get("horizontalTravelM")),
            "baselineEligible": baseline_eligible,
            "comparable": comparable,
        }
        result["comparison"] = {
            "comparable": comparable,
            "physicsEngineMatches": physics_matches,
            "authorityScopeMatches": authority_matches,
            "baselinePassedAllGates": baseline_eligible,
            "attackPoseTimelinePassed": attack_pose_passed,
            "attackActuatorNeutralizationPassed": attack_actuator_passed,
            "horizontalTravelDeltaM": round(
                measured["maxHorizontalTravelM"] - result["baseline"]["maxHorizontalTravelM"], 6
            )
            if comparable
            and isinstance(measured.get("maxHorizontalTravelM"), (int, float))
            and isinstance(result["baseline"].get("maxHorizontalTravelM"), (int, float))
            else None,
            "peakAltitudeDeltaM": round(
                measured["peakAltitudeM"] - result["baseline"]["peakAltitudeM"], 6
            )
            if comparable
            and isinstance(measured.get("peakAltitudeM"), (int, float))
            and isinstance(result["baseline"].get("peakAltitudeM"), (int, float))
            else None,
        }
        if not comparable:
            result["comparison"]["qualification"] = (
                "Runs are not eligible for a quantitative comparison; inspect engine, authority, flight, pose, and actuator gates for both runs."
            )

    output = run_dir / "research-result.json"
    output.write_text(json.dumps(result, indent=2) + "\n")
    manifest = build(run_dir)
    write_manifest(run_dir, manifest)
    print(output)
    return 0 if attack_impact_qualified else 2


if __name__ == "__main__":
    raise SystemExit(main())
