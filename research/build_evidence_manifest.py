#!/usr/bin/env python3
"""Build a redacted, integrity-checkable index of one Antoine research run."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

from dvd_research.evidence_contract import (
    classify_measurements,
    contains_symlink_component,
    sign_manifest_if_configured,
)
from dvd_research.scenario_catalog import load_scenario_catalog

PUBLIC_EVIDENCE = {
    "research-result.json",
    "research-baseline-summary.json",
    "research-waypoint-injection.json",
    "research-waypoint-replay-summary.json",
    "research-waypoint-replay.json",
    "research-nats-observation.json",
    "research-gps-tampering.json",
    "research-gps-tampering-summary.json",
    "research-early-revoke.json",
    "research-camera-observation.json",
    "research-sequence-poisoning.json",
    "research-sequence-poisoning-summary.json",
    "research-swarm-flight.json",
    "research-swarm-plan.json",
    "research-swarm-fault.json",
    "research-swarm-rate-disruption.json",
    "research-mavlink-swarm.json",
    "research-swarm-trajectory.svg",
    "research-swarm-config.json",
    "research-cross-vehicle-authorization.json",
    "research-cross-vehicle-independent-window.json",
    "research-swarm-selected-vehicle-authorization.json",
    "camera-world.json",
    "camera-world.sdf",
    "sensor-profile.json",
    "gz-flight-proof-report.json",
    "flight-evidence.json",
    "gazebo-pose.jsonl",
    "physics-engine.txt",
    "applier-neutral/actuator-output.jsonl",
    "applier-neutral/actuator-lifecycle.jsonl",
    "research-provenance-pre.json",
    "research-provenance-comparison.json",
    "research-provenance.json",
    "research-runtime-world.sdf",
    "research-gazebo-source-snapshot.json",
    "research-antoine-build-receipt.json",
    "antoine_sitl_nats.build.json",
    "research-antoine-process-launches.tsv",
    "research-antoine-progress.json",
    "research-sensor-profile-launcher.sh",
}


def scenario_catalog() -> list[dict[str, Any]]:
    path = Path(__file__).with_name("scenario-catalog.json")
    root = Path(__file__).resolve().parents[1]
    guide_root = root / "simulator" / "mgmt" / "templates" / "pages" / "attacks" / "antoine"
    scenarios = load_scenario_catalog(path, root, guide_root)
    return [
        {
            "scenario": item["id"],
            **{
                key: item.get(key)
                for key in ("label", "status", "runner", "runnerArguments", "gap", "evidenceGates")
            },
        }
        for item in scenarios
    ]


SCENARIO_COVERAGE = scenario_catalog()


def read_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def build(run_dir: Path) -> dict[str, Any]:
    run_dir = run_dir.resolve()
    result = read_json(run_dir / "research-result.json")
    baseline = read_json(run_dir / "research-baseline-summary.json")
    sensor_tamper = read_json(run_dir / "research-gps-tampering-summary.json")
    replay = read_json(run_dir / "research-waypoint-replay-summary.json")
    observation = read_json(run_dir / "research-nats-observation.json")
    camera = read_json(run_dir / "research-camera-observation.json")
    sequence_summary = read_json(run_dir / "research-sequence-poisoning-summary.json")
    early_revoke = read_json(run_dir / "research-early-revoke.json")
    swarm_flight = read_json(run_dir / "research-swarm-flight.json")
    swarm_plan = read_json(run_dir / "research-swarm-plan.json")
    swarm_plan = swarm_plan if isinstance(swarm_plan, dict) else {}
    provenance = read_json(run_dir / "research-provenance.json")
    provenance_comparison = read_json(run_dir / "research-provenance-comparison.json")
    flight = read_json(run_dir / "gz-flight-proof-report.json")
    source = (
        result
        or early_revoke
        or sensor_tamper
        or sequence_summary
        or camera
        or baseline
        or replay
        or observation
        or swarm_flight
        or {}
    )
    if source is observation:
        source = {
            **observation,
            "scenario": observation.get("scenario", "nats-readable-subjects-antoine"),
        }

    if swarm_flight:
        source = swarm_flight

    measurements: dict[str, Any] = {}
    if swarm_flight:
        measurements = {
            "runIdentityMatches": swarm_flight.get("allVehicleRunIdentityMatches") is True,
            "swarmFlightPassed": swarm_flight.get("status") == "passed"
            and swarm_flight.get("allFlightsPassed") is True,
            "swarmFlightStatus": swarm_flight.get("status", "unavailable"),
            "baselineQualified": swarm_flight.get("baselineStatus") == "qualified_baseline",
            "gazeboAttitudeQualified": (
                isinstance(swarm_flight.get("baselineGates"), dict)
                and swarm_flight["baselineGates"].get("gazeboAttitudeWithinEnvelope") is True
            ),
            "poseStreamQualityPassed": swarm_flight.get("poseStreamQualityPassed") is True,
            "baselineStatus": swarm_flight.get("baselineStatus", "unavailable"),
            "baselineGates": swarm_flight.get("baselineGates"),
            "allPairwiseSeparationsMeasured": swarm_flight.get("allPairwiseSeparationsMeasured")
            is True,
            "allPairwiseSeparationsAtLeast1m": swarm_flight.get("allPairwiseSeparationsAtLeast1m")
            is True,
            "vehicleCount": swarm_flight.get("vehicleCount"),
            "vehiclePairCount": swarm_flight.get("vehiclePairCount"),
            "minimumInterDroneSeparationM": swarm_flight.get("minimumInterDroneSeparationM"),
            "swarmBehavior": swarm_flight.get("behavior"),
            "behaviorPassed": swarm_flight.get("behaviorPassed") is True,
            "swarmPlanVersion": swarm_plan.get("version"),
            "altitudeLayerM": swarm_plan.get("altitudeLayerM"),
            "plannedSynchronizedClearanceM": swarm_plan.get("plannedSynchronizedClearanceM"),
            "plannedStaticPathClearanceM": swarm_plan.get("plannedStaticPathClearanceM"),
            "behaviorGates": swarm_flight.get("behaviorEvaluation", {}).get("gates")
            if isinstance(swarm_flight.get("behaviorEvaluation"), dict)
            else None,
            "mavlinkSwarmRequested": swarm_flight.get("mavlinkSwarmRequested") is True,
            "mavlinkSwarmPassed": swarm_flight.get("mavlinkSwarmPassed"),
            "mavlinkVehiclesPassed": swarm_flight.get("mavlinkSwarmObservation", {}).get(
                "vehiclesPassed"
            )
            if isinstance(swarm_flight.get("mavlinkSwarmObservation"), dict)
            else None,
            "faultObservationStatus": swarm_flight.get("faultObservation", {}).get("status")
            if isinstance(swarm_flight.get("faultObservation"), dict)
            else None,
        }
    elif result:
        attack = result.get("attack", {})
        actuator = result.get("applier", {})
        gazebo = result.get("independentGazebo", {})
        comparison = result.get("comparison", {})
        baseline_result = result.get("baseline", {})
        attack = attack if isinstance(attack, dict) else {}
        actuator = actuator if isinstance(actuator, dict) else {}
        gazebo = gazebo if isinstance(gazebo, dict) else {}
        comparison = comparison if isinstance(comparison, dict) else {}
        baseline_result = baseline_result if isinstance(baseline_result, dict) else {}
        measurements = {
            "publishedMessages": attack.get("publishedMessages"),
            "parserAcceptedMutation": attack.get("setpointParserAcceptedMutation"),
            "runIdentityMatches": attack.get("runIdentityMatches"),
            "injectionImpactQualified": attack.get("impactQualified"),
            "neutralOutputObserved": actuator.get("neutralActuatorOutputObserved"),
            "neutralizationPassed": actuator.get("actuatorNeutralizationPassed"),
            "localFlightPassed": gazebo.get("localFlightPassed"),
            "allLegsReached": gazebo.get("allLegsReached"),
            "peakAltitudeM": gazebo.get("peakAltitudeM"),
            "horizontalTravelM": gazebo.get("maxHorizontalTravelM"),
            "publishToStopMs": attack.get("publishToStopMs"),
            "baselineComparable": comparison.get("comparable"),
            "baselineHorizontalTravelM": baseline_result.get("maxHorizontalTravelM"),
            "horizontalTravelDeltaM": comparison.get("horizontalTravelDeltaM"),
            "baselinePeakAltitudeM": baseline_result.get("peakAltitudeM"),
            "peakAltitudeDeltaM": comparison.get("peakAltitudeDeltaM"),
        }
    elif baseline:
        baseline_measurements = baseline.get("measurements", {})
        baseline_gates = baseline.get("gates", {})
        baseline_measurements = (
            baseline_measurements if isinstance(baseline_measurements, dict) else {}
        )
        baseline_gates = baseline_gates if isinstance(baseline_gates, dict) else {}
        measurements = {
            **baseline_measurements,
            "baselineEligible": baseline.get("baselineEligible"),
            "physicalSafeStopPassed": baseline_gates.get("physicalSafeStopPassed") is True,
            "groundSettlePassed": baseline_gates.get("groundSettlePassed") is True,
            "runIdentityMatches": baseline_gates.get("runIdentityMatches") is True,
            "localFlightPassed": baseline_gates.get("localFlightPassed"),
            "allLegsReached": baseline_measurements.get("allLegsReached"),
            "peakAltitudeM": baseline_measurements.get("peakAltitudeM"),
            "horizontalTravelM": baseline_measurements.get("horizontalTravelM"),
            "actuatorCommands": baseline_measurements.get("actuatorCommands"),
            "neutralRows": baseline_measurements.get("neutralRows"),
        }
    elif early_revoke:
        measurements = {
            "revokeToNeutralMs": early_revoke.get("revokeToNeutralMs"),
            "localFlightPassed": early_revoke.get("localFlightPassed"),
            "allLegsReached": early_revoke.get("allLegsReached"),
            "revokeQualified": all(early_revoke.get("gates", {}).values())
            if isinstance(early_revoke.get("gates"), dict)
            else False,
            "runIdentityMatches": early_revoke.get("gates", {}).get("runIdentityMatches") is True
            if isinstance(early_revoke.get("gates"), dict)
            else False,
        }
    elif sensor_tamper:
        sensor_flight = sensor_tamper.get("flightProof", {})
        sensor_flight = sensor_flight if isinstance(sensor_flight, dict) else {}
        sensor_measurements = sensor_tamper.get("measurements", {})
        sensor_measurements = sensor_measurements if isinstance(sensor_measurements, dict) else {}
        measurements = {
            "publishedMessages": sensor_tamper.get("publishedMessages"),
            "tamperingRejectedObserved": sensor_tamper.get("tamperingRejectedObserved"),
            "gpsReplayObservationQualified": sensor_tamper.get("gpsReplayObservationQualified")
            is True,
            "gpsReplayCase": sensor_tamper.get("case"),
            "validFixtureSignature": sensor_tamper.get("validFixtureSignature") is True,
            "sensorIngressAccepted": sensor_tamper.get("sensorIngressAccepted"),
            "freshnessUseDecisionObserved": sensor_tamper.get("freshnessUseDecisionObserved")
            is True,
            "runIdentityMatches": sensor_tamper.get("artifactRunIds", {}).get("tampering")
            == sensor_tamper.get("runId")
            == run_dir.name,
            "gpsTamperingQualified": sensor_tamper.get("tamperingQualified") is True,
            "acceptedGpsTraceDelta": sensor_tamper.get("acceptedGpsTraceDelta"),
            "cameraFramesCaptured": camera.get("capturedMessages") if camera else None,
            "cameraTopicListed": camera.get("topicListed") if camera else None,
            "cameraPublisherPresent": camera.get("publisherPresentAtProbe") if camera else None,
            "cameraDimensions": camera.get("dimensions") if camera else None,
            "cameraObservationStatus": camera.get("interpretation") if camera else None,
            "localFlightPassed": sensor_flight.get(
                "localFlightPassed", sensor_measurements.get("localFlightPassed")
            ),
            "allLegsReached": sensor_flight.get(
                "allLegsReached", sensor_measurements.get("allLegsReached")
            ),
            "peakAltitudeM": sensor_flight.get(
                "peakAltitudeM", sensor_measurements.get("peakAltitudeM")
            ),
            "horizontalTravelM": sensor_flight.get(
                "maxHorizontalTravelM", sensor_measurements.get("horizontalTravelM")
            ),
        }
    elif replay:
        replay_data = replay.get("replay", {})
        gazebo = replay.get("independentGazebo", {})
        measurements = {
            "publishedMessages": replay_data.get("publishedMessages"),
            "antiReplayRejectionObserved": replay_data.get("antiReplayRejectionObserved"),
            "replayQualified": replay.get("replayQualified") is True,
            "runIdentityMatches": replay.get("runIdentityMatches") is True,
            "localFlightPassed": gazebo.get("localFlightPassed"),
            "allLegsReached": gazebo.get("allLegsReached"),
            "peakAltitudeM": gazebo.get("peakAltitudeM"),
            "horizontalTravelM": gazebo.get("maxHorizontalTravelM"),
        }
    elif sequence_summary:
        gates = sequence_summary.get("gates", {})
        gates = gates if isinstance(gates, dict) else {}
        measurements = {
            "runIdentityMatches": gates.get("runIdentityMatches") is True,
            "sequenceRecoveryObservationQualified": sequence_summary.get("qualification")
            == "bounded_sequence_recovery_observed",
            "highWaterSequence": sequence_summary.get("highWaterSequence"),
            "recoverySequence": sequence_summary.get("recoverySequence"),
            "recoveryToActuatorProgressMs": sequence_summary.get("recoveryToActuatorProgressMs"),
            "allLegsReached": sequence_summary.get("allLegsReached"),
            "horizontalTravelM": sequence_summary.get("horizontalTravelM"),
        }
    elif observation:
        subjects = observation.get("subjects", [])
        messages = [
            {"subject": subject.get("subject"), "count": subject.get("capturedMessages", 0)}
            for subject in subjects
            if isinstance(subject, dict)
        ]
        measurements = {
            "capturedMessages": sum(entry["count"] for entry in messages),
            "subjects": sorted({entry["subject"] for entry in messages if entry["subject"]}),
            "flightProofPassed": observation.get("flightProof", {}).get("localFlightPassed"),
            "runIdentityMatches": observation.get("flightProof", {}).get("runIdentityMatches")
            is True,
            "observationBoundsValid": (
                len(subjects) == 4
                and {item.get("subject") for item in subjects if isinstance(item, dict)}
                == {
                    "furia.gzsitl.setpoint.drone-1",
                    "furia.prime.execution-grant.drone-1",
                    "furia.gzsitl.state.drone-1",
                    "brenta.swarm.0.vehicle.drone-1.action-outcome",
                }
                and all(
                    type(item.get("capturedMessages")) is int and 0 <= item["capturedMessages"] <= 1
                    for item in subjects
                    if isinstance(item, dict)
                )
            ),
        }
    elif camera:
        measurements = {
            "capturedMessages": camera.get("capturedMessages"),
            "cameraFramesCaptured": camera.get("capturedMessages"),
            "cameraTopicListed": camera.get("topicListed"),
            "cameraPublisherPresent": camera.get("publisherPresentAtProbe"),
            "cameraDimensions": camera.get("dimensions"),
            "cameraObservationStatus": camera.get("interpretation"),
            "dimensions": camera.get("dimensions"),
            "payloadSizes": camera.get("payloadSizes"),
            "imagePayloadsStored": camera.get("imagePayloadsStored"),
            "runIdentityMatches": camera.get("runId") == run_dir.name,
            "cameraObservationBoundsValid": (
                type(camera.get("capturedMessages")) is int
                and 0 <= camera.get("capturedMessages") <= 5
                and camera.get("imagePayloadsStored") is False
                and camera.get("topic")
                == "/world/antoine_gz_flight_proof/model/isr/link/base_link/sensor/camera/image"
            ),
        }
    elif flight:
        measured = flight.get("measured", {})
        measurements = {
            "localFlightPassed": flight.get("localFlightPassed"),
            "allLegsReached": measured.get("allLegsReached"),
            "peakAltitudeM": measured.get("peakAltitudeM"),
            "horizontalTravelM": measured.get("maxHorizontalTravelM"),
        }

    measurements["provenanceStable"] = bool(
        provenance_comparison
        and provenance_comparison.get("runId") == run_dir.name
        and provenance_comparison.get("prePhaseValid") is True
        and provenance_comparison.get("postPhaseValid") is True
        and provenance_comparison.get("profileMatches") is True
        and provenance_comparison.get("stable") is True
    )

    evidence_paths = set(PUBLIC_EVIDENCE)
    evidence_paths.update(
        f"applier-neutral/drone-{index}.armed-state.json" for index in range(1, 6)
    )
    gazebo_snapshot = run_dir / "gazebo-source-snapshot"
    if gazebo_snapshot.is_dir() and not gazebo_snapshot.is_symlink():
        evidence_paths.update(
            path.relative_to(run_dir).as_posix()
            for path in gazebo_snapshot.rglob("*")
            if path.is_file() and not path.is_symlink()
        )
    for snapshot_name in ("antoine-source-snapshot", "antoine-binaries"):
        snapshot_dir = run_dir / snapshot_name
        if snapshot_dir.is_dir() and not snapshot_dir.is_symlink():
            evidence_paths.update(
                path.relative_to(run_dir).as_posix()
                for path in snapshot_dir.rglob("*")
                if path.is_file() and not path.is_symlink()
            )
    logs_dir = run_dir / "logs"
    if logs_dir.is_dir() and not logs_dir.is_symlink():
        evidence_paths.update(
            path.relative_to(run_dir).as_posix()
            for pattern in ("local-authority-drone-*.log", "antoine-controller-drone-*.log")
            for path in logs_dir.glob(pattern)
            if path.is_file() and not path.is_symlink()
        )
    evidence_paths.update(
        path.relative_to(run_dir).as_posix()
        for path in run_dir.glob("flight-evidence-drone-*.json")
        if path.is_file() and not path.is_symlink()
    )
    available = []
    for name in sorted(evidence_paths):
        path = run_dir / name
        try:
            safe_file = path.is_file() and not contains_symlink_component(run_dir, Path(name))
            if safe_file:
                path.resolve(strict=True).relative_to(run_dir)
        except (OSError, ValueError):
            safe_file = False
        if safe_file:
            available.append(
                {"name": name, "sizeBytes": path.stat().st_size, "sha256": sha256(path)}
            )

    measurement_classification = classify_measurements(measurements)
    qualification = source.get("qualification", source.get("qualificationNote"))
    if measurement_classification == "failed_swarm_flight":
        qualification = f"Measured swarm failure. {qualification or ''}".strip()
    elif measurement_classification == "inconclusive":
        if measurements.get("provenanceStable") is not True:
            reason = "pre/post provenance is missing or unstable"
        elif measurements.get("runIdentityMatches") is not True:
            reason = "run identity does not match"
        else:
            reason = "one or more scenario qualification gates failed"
        qualification = f"Inconclusive: {reason}. {qualification or ''}".strip()

    manifest = {
        "contract": "damn-vulnerable-drone.antoine-evidence-manifest",
        "version": "1.0.0",
        "runId": source.get("runId") or run_dir.name,
        "scenario": source.get("scenario", run_dir.name),
        "scope": (
            "same-host Gazebo Transport in an isolated camera-only partition; no NATS or Antoine controller"
            if source.get("scenario") == "gazebo-camera-readable-topic-antoine"
            else "run-owned NATS on loopback and X500 in local Gazebo only"
        ),
        "physicsEngine": (run_dir / "physics-engine.txt").read_text(encoding="utf-8").strip()
        if (run_dir / "physics-engine.txt").is_file()
        else None,
        "qualification": qualification,
        "measurementClassification": measurement_classification,
        "interpretation": source.get("interpretation"),
        "measurements": measurements,
        "flightProof": {
            "available": flight is not None,
            "exitCode": observation.get("flightProof", {}).get("exitCode") if observation else None,
            "localFlightPassed": flight.get("localFlightPassed") if flight else None,
        },
        "publicEvidence": available,
        "sourceProvenance": {
            "available": provenance is not None,
            "stable": measurements.get("provenanceStable") is True,
            "comparison": provenance_comparison or {},
            "sourceTrees": provenance.get("sourceTrees", []) if provenance else [],
            "inputs": provenance.get("inputs", []) if provenance else [],
            "limitations": provenance.get("limitations", []) if provenance else [],
        },
        "scenarioCoverage": scenario_catalog(),
        "excludedByDesign": [
            "run-keys.txt",
            "actuation-sequence.bin",
            "actuation-sequence.sequence.lock",
            "launcher logs",
        ],
    }
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("run_dir", type=Path)
    args = parser.parse_args()
    manifest = build(args.run_dir)
    output = write_manifest(args.run_dir, manifest)
    print(output)
    return 0


def write_manifest(run_dir: Path, manifest: dict[str, Any]) -> Path:
    output = Path(run_dir) / "research-evidence-manifest.json"
    output.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    sign_manifest_if_configured(output)
    return output


if __name__ == "__main__":
    raise SystemExit(main())
