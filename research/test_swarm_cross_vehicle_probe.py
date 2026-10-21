"""Tests for one-field cross-vehicle setpoint mutations."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from swarm_cross_vehicle_probe import (
    actuator_numeric_vector,
    controller_decision,
    mutate_setpoint,
    pose_window_at_target,
    vehicle_effect_snapshot,
    write_report,
)


def valid_live_setpoint() -> dict:
    return {
        "contract": "furia.gzsitl.mission-setpoint",
        "version": "2.1.0",
        "vehicleId": "drone-2",
        "frameId": "enu-test",
        "missionId": "mission-drone-2",
        "graphId": "graph-drone-2",
        "taskId": "task-drone-2",
        "sourceRevision": "source-revision",
        "planRevision": "plan-revision",
        "planDigest": "sha256:plan",
        "taskKind": "navigate",
        "parametersDigest": "sha256:parameters",
        "taskSourceArtifactSha256": "a" * 64,
        "assignmentDigest": "sha256:assignment",
        "stepIndex": 1,
        "taskCapabilityMapRevision": "map-revision",
        "taskCapabilityMapDigest": "sha256:capabilities",
        "operation": "navigate",
        "capability": "position-target",
        "targetShape": "point-3d",
        "vigieAdmissionCorrelationId": (
            "vigie-intent-mission-drone-2-graph-drone-2-task-drone-2-12"
        ),
        "vigieAdmissionSequence": 12,
        "executionEpoch": 1,
        "authorityEpoch": 1,
        "sequence": 30,
        "issuedAtMs": 1_000,
        "expiresAtMs": 2_500,
        "position_enu_m": [8.0, 8.0, 10.0],
    }


class MutationTests(unittest.TestCase):
    PROBE_HASH = "a" * 64

    def test_actuator_vector_rejects_partial_or_nonfinite_values(self) -> None:
        self.assertIsNone(actuator_numeric_vector({"left": 0.2, "right": "unknown"}))
        self.assertIsNone(actuator_numeric_vector({"left": [0.2, float("nan")]}))
        self.assertEqual(actuator_numeric_vector({"left": [0.2, 0.3]}), [0.2, 0.3])

    def test_wrong_callsign_changes_only_vehicle_binding_and_freshness_sequence(self) -> None:
        original = valid_live_setpoint()
        mutated, evidence = mutate_setpoint(original, "wrong-callsign", "drone-1", "drone-2")
        self.assertEqual(mutated["vehicleId"], "drone-1")
        self.assertEqual(mutated["sequence"], 130)
        self.assertEqual(evidence["sequenceGap"], 100)
        for field in original.keys() - {"vehicleId", "sequence"}:
            self.assertEqual(mutated[field], original[field], field)
        self.assertEqual(evidence["field"], "vehicleId")
        self.assertTrue(evidence["sequenceAdvancedForFreshness"])

    def test_wrong_task_changes_only_task_binding_and_freshness_sequence(self) -> None:
        original = valid_live_setpoint()
        mutated, evidence = mutate_setpoint(original, "wrong-task", "drone-1", "drone-2")
        self.assertNotEqual(mutated["taskId"], original["taskId"])
        self.assertNotEqual(
            mutated["vigieAdmissionCorrelationId"], original["vigieAdmissionCorrelationId"]
        )
        self.assertEqual(mutated["sequence"], 130)
        for field in original.keys() - {"taskId", "sequence", "vigieAdmissionCorrelationId"}:
            self.assertEqual(mutated[field], original[field], field)
        self.assertEqual(evidence["field"], "taskId")
        self.assertEqual(evidence["additionalFields"], ["vigieAdmissionCorrelationId"])

    def test_wrong_target_changes_only_one_bounded_position_component_and_sequence(self) -> None:
        original = valid_live_setpoint()
        mutated, evidence = mutate_setpoint(original, "wrong-target", "drone-1", "drone-2")
        self.assertEqual(mutated["position_enu_m"], [10.0, 8.0, 10.0])
        self.assertEqual(mutated["sequence"], 130)
        for field in original.keys() - {"position_enu_m", "sequence"}:
            self.assertEqual(mutated[field], original[field], field)
        self.assertEqual(evidence["field"], "position_enu_m")

    def test_stale_run_is_explicitly_unsupported_by_setpoint_contract(self) -> None:
        payload, evidence = mutate_setpoint({}, "stale-run", "drone-1", "drone-2")
        self.assertIsNone(payload)
        self.assertEqual(evidence["status"], "unsupported")
        self.assertEqual(evidence["field"], "runId")

    def test_rejects_payload_not_bound_to_requested_target(self) -> None:
        payload = valid_live_setpoint()
        payload["vehicleId"] = "drone-1"
        with self.assertRaises(ValueError):
            mutate_setpoint(payload, "wrong-task", "drone-1", "drone-2")

    def test_wrong_callsign_decision_requires_rejection_without_candidate_acceptance(self) -> None:
        lines = [
            f"ANTOINE_GZ_SETPOINT_REJECTED callsign=drone-2 reason=current-grant-or-vigie-binding vehicle_id=drone-1 sequence=31 payload_sha256={self.PROBE_HASH} last_sequence=30",
        ]
        decision = controller_decision(
            "wrong-callsign", lines, 31, "drone-2", "drone-1", self.PROBE_HASH
        )
        self.assertEqual(decision["status"], "rejected")
        self.assertFalse(decision["candidateSetpointReceived"])

    def test_wrong_callsign_decision_correlates_unique_sequence_when_legacy_log_lacks_hash(
        self,
    ) -> None:
        decision = controller_decision(
            "wrong-callsign",
            [
                "ANTOINE_GZ_SETPOINT_REJECTED callsign=drone-2 reason=vehicle-or-sequence vehicle_id=drone-1 sequence=130 last_sequence=30"
            ],
            130,
            "drone-2",
            "drone-1",
            self.PROBE_HASH,
        )
        self.assertEqual(decision["status"], "rejected")
        self.assertEqual(decision["correlation"], "unique-sequence-and-vehicle-id")

    def test_wrong_task_decision_requires_rejection_before_active_setpoint(self) -> None:
        lines = [
            f"ANTOINE_GZ_SETPOINT_REJECTED callsign=drone-2 reason=current-grant-or-vigie-binding vehicle_id=drone-2 sequence=31 payload_sha256={self.PROBE_HASH} last_sequence=30",
        ]
        decision = controller_decision(
            "wrong-task",
            lines,
            31,
            "drone-2",
            "dvd-cross-vehicle-unauthorized-drone-2",
            self.PROBE_HASH,
        )
        self.assertEqual(decision["status"], "rejected-before-active-setpoint")
        self.assertTrue(decision["activeSetpointSequenceNotAdvanced"])

    def test_wrong_target_decision_requires_rejection_before_active_setpoint(self) -> None:
        lines = [
            f"ANTOINE_GZ_SETPOINT_REJECTED callsign=drone-2 reason=current-grant-or-vigie-binding vehicle_id=drone-2 sequence=31 payload_sha256={self.PROBE_HASH} last_sequence=30",
        ]
        decision = controller_decision(
            "wrong-target", lines, 31, "drone-2", "unused", self.PROBE_HASH
        )
        self.assertEqual(decision["status"], "rejected-before-active-setpoint")
        self.assertTrue(decision["activeSetpointSequenceNotAdvanced"])

    def test_wrong_callsign_rejection_must_match_exact_sequence_and_vehicle(self) -> None:
        for sequence, vehicle in ((310, "drone-1"), (31, "drone-10")):
            with self.subTest(sequence=sequence, vehicle=vehicle):
                lines = [
                    f"ANTOINE_GZ_SETPOINT_REJECTED callsign=drone-2 reason=vehicle-or-sequence vehicle_id={vehicle} sequence={sequence} payload_sha256={self.PROBE_HASH} last_sequence=30"
                ]
                decision = controller_decision(
                    "wrong-callsign", lines, 31, "drone-2", "drone-1", self.PROBE_HASH
                )
                self.assertEqual(decision["status"], "inconclusive")

    def test_wrong_callsign_rejection_with_candidate_acceptance_is_inconclusive(self) -> None:
        lines = [
            f"ANTOINE_GZ_SETPOINT_REJECTED callsign=drone-2 reason=vehicle-or-sequence vehicle_id=drone-1 sequence=31 payload_sha256={self.PROBE_HASH} last_sequence=30",
            f"ANTOINE_GZ_SETPOINT_RECEIVED callsign=drone-2 sequence=31 payload_sha256={self.PROBE_HASH} accepted_at_unix_ms=2000",
        ]
        decision = controller_decision(
            "wrong-callsign", lines, 31, "drone-2", "drone-1", self.PROBE_HASH
        )
        self.assertEqual(decision["status"], "inconclusive")
        self.assertTrue(decision["candidateSetpointReceived"])


class PoseTruthTests(unittest.TestCase):
    def test_report_replacement_failure_preserves_previous_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            report_path = Path(directory) / "report.json"
            report_path.write_text('{"status":"previous"}\n', encoding="utf-8")

            with patch("swarm_cross_vehicle_probe.os.replace", side_effect=OSError("disk error")):
                with self.assertRaisesRegex(OSError, "disk error"):
                    write_report(report_path, {"status": "new"})

            self.assertEqual(report_path.read_text(encoding="utf-8"), '{"status":"previous"}\n')
            self.assertEqual(list(Path(directory).glob(".report.json.*.tmp")), [])

    def test_non_object_actuator_rows_are_skipped_and_reported_missing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory)
            actuator_path = run_dir / "applier-neutral" / "actuator-output.jsonl"
            actuator_path.parent.mkdir()
            actuator_path.write_text('[]\nnull\n"not-an-object"\n', encoding="utf-8")

            snapshot = vehicle_effect_snapshot(run_dir, {"drone-1": "fpv-a"})

        self.assertEqual(snapshot["actuatorByVehicle"], {})
        self.assertIn("actuator:drone-1", snapshot["missing"])

    def test_nonfinite_or_reversed_pose_stream_cannot_prove_dwell(self) -> None:
        for invalid in ("nan", "infinity", "reversed", "duplicate"):
            with self.subTest(invalid=invalid), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "gazebo-pose.jsonl"
                frames = []
                for index in range(61):
                    tick = 60 - index if invalid == "reversed" else index
                    if invalid == "duplicate":
                        tick = 0
                    frames.append(
                        {
                            "header": {"stamp": {"sec": 10, "nsec": tick * 20_000_000}},
                            "pose": [
                                {
                                    "name": "fpv-a",
                                    "position": {
                                        "x": float("nan")
                                        if invalid == "nan"
                                        else float("inf")
                                        if invalid == "infinity"
                                        else 8.0,
                                        "y": 8.0,
                                        "z": 10.0,
                                    },
                                }
                            ],
                        }
                    )
                path.write_text("".join(json.dumps(frame) + "\n" for frame in frames))
                self.assertIsNone(pose_window_at_target(path, "fpv-a", [8.0, 8.0, 10.0]))

    def test_pose_window_requires_continuous_dwell_at_target(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "gazebo-pose.jsonl"
            with path.open("w", encoding="utf-8") as stream:
                for index in range(61):
                    stream.write(
                        json.dumps(
                            {
                                "header": {"stamp": {"sec": "10", "nsec": index * 20_000_000}},
                                "pose": [
                                    {
                                        "name": "fpv-a",
                                        "position": {"x": 8.0, "y": 8.1, "z": 10.0},
                                    }
                                ],
                            }
                        )
                        + "\n"
                    )
            evidence = pose_window_at_target(path, "fpv-a", [8.0, 8.0, 10.0])
        self.assertIsNotNone(evidence)
        self.assertGreaterEqual(evidence["dwellSeconds"], 1.0)

    def test_pose_window_rejects_large_gaps(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "gazebo-pose.jsonl"
            frames = [
                {
                    "header": {"stamp": {"sec": "1", "nsec": 0}},
                    "pose": [{"name": "fpv-a", "position": {"x": 8, "y": 8, "z": 10}}],
                },
                {
                    "header": {"stamp": {"sec": "2", "nsec": 0}},
                    "pose": [{"name": "fpv-a", "position": {"x": 8, "y": 8, "z": 10}}],
                },
            ]
            path.write_text("".join(json.dumps(frame) + "\n" for frame in frames), encoding="utf-8")
            evidence = pose_window_at_target(path, "fpv-a", [8.0, 8.0, 10.0])
        self.assertIsNone(evidence)


if __name__ == "__main__":
    unittest.main()
