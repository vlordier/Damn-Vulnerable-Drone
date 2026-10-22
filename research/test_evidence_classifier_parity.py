"""Shared classification fixtures for UI and CSV evidence surfaces."""

from __future__ import annotations

import ast
import json
import tempfile
import unittest
from pathlib import Path

from build_evidence_manifest import build as build_evidence_manifest
from dvd_research.evidence_contract import classify_measurements

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = (
    (
        "baseline",
        {
            "provenanceStable": True,
            "runIdentityMatches": True,
            "baselineEligible": True,
            "physicalSafeStopPassed": False,
            "groundSettlePassed": True,
        },
        "qualified_baseline",
    ),
    (
        "waypoint injection",
        {
            "provenanceStable": True,
            "runIdentityMatches": True,
            "injectionImpactQualified": True,
        },
        "qualified_attack_observation",
    ),
    (
        "signature mutation",
        {
            "provenanceStable": True,
            "runIdentityMatches": True,
            "gpsTamperingQualified": True,
            "tamperingRejectedObserved": True,
        },
        "qualified_signature_mutation_rejection",
    ),
    (
        "replay rejection",
        {
            "provenanceStable": True,
            "runIdentityMatches": True,
            "replayQualified": True,
            "antiReplayRejectionObserved": True,
        },
        "qualified_replay_rejection",
    ),
    (
        "local revoke",
        {
            "provenanceStable": True,
            "runIdentityMatches": True,
            "revokeQualified": True,
        },
        "qualified_local_revoke_stop_observation",
    ),
    (
        "camera observation",
        {
            "provenanceStable": True,
            "runIdentityMatches": True,
            "cameraObservationBoundsValid": True,
            "cameraFramesCaptured": 5,
        },
        "observation_only",
    ),
    (
        "read-only observation",
        {
            "provenanceStable": True,
            "runIdentityMatches": True,
            "observationBoundsValid": True,
            "capturedMessages": 4,
        },
        "observation_only",
    ),
    (
        "sequence recovery observation",
        {
            "provenanceStable": True,
            "runIdentityMatches": True,
            "sequenceRecoveryObservationQualified": True,
        },
        "observation_only",
    ),
    (
        "measured swarm failure",
        {
            "provenanceStable": True,
            "runIdentityMatches": True,
            "swarmFlightStatus": "failed",
            "baselineQualified": False,
            "gazeboAttitudeQualified": False,
        },
        "failed_swarm_flight",
    ),
    (
        "swarm flight without behaviour evidence",
        {
            "provenanceStable": True,
            "runIdentityMatches": True,
            "swarmFlightStatus": "passed",
            "swarmFlightPassed": True,
            "poseStreamQualityPassed": True,
            "allPairwiseSeparationsMeasured": True,
            "allPairwiseSeparationsAtLeast1m": True,
        },
        "inconclusive",
    ),
    (
        "swarm flight with measured attitude breach",
        {
            "provenanceStable": True,
            "runIdentityMatches": True,
            "swarmFlightStatus": "passed",
            "swarmFlightPassed": True,
            "baselineQualified": True,
            "gazeboAttitudeQualified": False,
            "poseStreamQualityPassed": True,
            "allPairwiseSeparationsMeasured": True,
            "allPairwiseSeparationsAtLeast1m": True,
            "behaviorPassed": True,
            "mavlinkSwarmPassed": None,
        },
        "inconclusive",
    ),
    (
        "swarm flight without complete baseline evidence",
        {
            "provenanceStable": True,
            "runIdentityMatches": True,
            "swarmFlightStatus": "passed",
            "swarmFlightPassed": True,
            "baselineQualified": False,
            "gazeboAttitudeQualified": True,
            "poseStreamQualityPassed": True,
            "allPairwiseSeparationsMeasured": True,
            "allPairwiseSeparationsAtLeast1m": True,
            "behaviorPassed": True,
            "mavlinkSwarmPassed": None,
        },
        "inconclusive",
    ),
    (
        "scripted swarm behaviour with baseline and attitude evidence",
        {
            "provenanceStable": True,
            "runIdentityMatches": True,
            "swarmFlightStatus": "passed",
            "swarmFlightPassed": True,
            "baselineQualified": True,
            "gazeboAttitudeQualified": True,
            "poseStreamQualityPassed": True,
            "allPairwiseSeparationsMeasured": True,
            "allPairwiseSeparationsAtLeast1m": True,
            "behaviorPassed": True,
            "mavlinkSwarmPassed": None,
        },
        "qualified_swarm_flight",
    ),
    (
        "swarm flight with failed MAVLink fan-out",
        {
            "provenanceStable": True,
            "runIdentityMatches": True,
            "swarmFlightStatus": "passed",
            "swarmFlightPassed": True,
            "baselineQualified": True,
            "gazeboAttitudeQualified": True,
            "poseStreamQualityPassed": True,
            "allPairwiseSeparationsMeasured": True,
            "allPairwiseSeparationsAtLeast1m": True,
            "behaviorPassed": True,
            "mavlinkSwarmPassed": False,
        },
        "inconclusive",
    ),
    (
        "swarm flight without pose stream quality",
        {
            "provenanceStable": True,
            "runIdentityMatches": True,
            "swarmFlightStatus": "passed",
            "swarmFlightPassed": True,
            "baselineQualified": True,
            "gazeboAttitudeQualified": True,
            "allPairwiseSeparationsMeasured": True,
            "allPairwiseSeparationsAtLeast1m": True,
            "behaviorPassed": True,
            "mavlinkSwarmPassed": None,
        },
        "inconclusive",
    ),
    (
        "missing provenance",
        {"runIdentityMatches": True, "baselineEligible": True},
        "inconclusive",
    ),
    (
        "baseline without measured ground dwell",
        {
            "provenanceStable": True,
            "runIdentityMatches": True,
            "baselineEligible": True,
        },
        "inconclusive",
    ),
)


def imported_classifier(path: Path) -> str | None:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "dvd_research.evidence_contract":
            for item in node.names:
                if item.name == "classify_measurements":
                    return item.asname or item.name
    return None


class EvidenceClassifierParityTests(unittest.TestCase):
    def test_scenario_families_share_expected_labels(self) -> None:
        for family, measurements, expected in FIXTURES:
            with self.subTest(family=family):
                self.assertEqual(classify_measurements(measurements), expected)

    def test_manifest_preserves_measured_swarm_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory) / "antoine-swarm-flight-manifest-test"
            run_dir.mkdir()
            report = {
                "runId": run_dir.name,
                "scenario": "independent-antoine-controllers-gazebo-nats",
                "status": "failed",
                "allVehicleRunIdentityMatches": True,
                "allFlightsPassed": False,
                "baselineStatus": "failed",
                "baselineGates": {"gazeboAttitudeWithinEnvelope": False},
                "poseStreamQualityPassed": True,
                "allPairwiseSeparationsMeasured": True,
                "allPairwiseSeparationsAtLeast1m": True,
                "behaviorPassed": True,
            }
            (run_dir / "research-swarm-flight.json").write_text(
                json.dumps(report), encoding="utf-8"
            )
            (run_dir / "research-provenance-comparison.json").write_text(
                json.dumps(
                    {
                        "runId": run_dir.name,
                        "prePhaseValid": True,
                        "postPhaseValid": True,
                        "profileMatches": True,
                        "stable": True,
                    }
                ),
                encoding="utf-8",
            )

            manifest = build_evidence_manifest(run_dir)

        self.assertFalse(manifest["measurements"]["gazeboAttitudeQualified"])
        self.assertEqual(manifest["measurementClassification"], "failed_swarm_flight")

    def test_manifest_does_not_qualify_pass_status_with_failed_attitude_gate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory) / "antoine-swarm-flight-manifest-invalid-test"
            run_dir.mkdir()
            report = {
                "runId": run_dir.name,
                "scenario": "independent-antoine-controllers-gazebo-nats",
                "status": "passed",
                "allVehicleRunIdentityMatches": True,
                "allFlightsPassed": True,
                "baselineStatus": "qualified_baseline",
                "baselineGates": {"gazeboAttitudeWithinEnvelope": False},
                "poseStreamQualityPassed": True,
                "allPairwiseSeparationsMeasured": True,
                "allPairwiseSeparationsAtLeast1m": True,
                "behaviorPassed": True,
            }
            (run_dir / "research-swarm-flight.json").write_text(
                json.dumps(report), encoding="utf-8"
            )
            (run_dir / "research-provenance-comparison.json").write_text(
                json.dumps(
                    {
                        "runId": run_dir.name,
                        "prePhaseValid": True,
                        "postPhaseValid": True,
                        "profileMatches": True,
                        "stable": True,
                    }
                ),
                encoding="utf-8",
            )

            manifest = build_evidence_manifest(run_dir)

        self.assertFalse(manifest["measurements"]["gazeboAttitudeQualified"])
        self.assertEqual(manifest["measurementClassification"], "inconclusive")

    def test_ui_and_csv_import_the_shared_classifier(self) -> None:
        consumers = (
            ROOT / "simulator" / "mgmt" / "routes" / "pages_attacks.py",
            ROOT / "research" / "export_research_csv.py",
        )
        for consumer in consumers:
            with self.subTest(consumer=consumer.name):
                self.assertEqual(imported_classifier(consumer), "classify_measurements")


if __name__ == "__main__":
    unittest.main()
