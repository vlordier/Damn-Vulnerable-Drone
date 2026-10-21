"""Shared classification fixtures for UI and CSV evidence surfaces."""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

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
        "swarm flight without behaviour evidence",
        {
            "provenanceStable": True,
            "runIdentityMatches": True,
            "swarmFlightPassed": True,
            "poseStreamQualityPassed": True,
            "allPairwiseSeparationsMeasured": True,
            "allPairwiseSeparationsAtLeast1m": True,
        },
        "inconclusive",
    ),
    (
        "scripted swarm behaviour",
        {
            "provenanceStable": True,
            "runIdentityMatches": True,
            "swarmFlightPassed": True,
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
            "swarmFlightPassed": True,
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
            "swarmFlightPassed": True,
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
