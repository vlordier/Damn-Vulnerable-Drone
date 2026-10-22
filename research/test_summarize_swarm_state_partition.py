from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from summarize_swarm_state_partition import actuator_vector, summarize


class StatePartitionSummaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.run_id = "antoine-swarm-flight-test"
        self.run_dir = Path(self.temp.name) / self.run_id
        (self.run_dir / "logs").mkdir(parents=True)
        (self.run_dir / "applier-neutral").mkdir()
        config = {
            "fault": "state-stream-partition",
            "faultCallsign": "drone-2",
            "vehicles": [
                {"callsign": "drone-1", "model": "model-a"},
                {"callsign": "drone-2", "model": "model-b"},
            ],
        }
        (self.run_dir / "research-swarm-config.json").write_text(json.dumps(config))
        controller = """ANTOINE_GZ_STATE_COALESCING_ENABLED callsign=drone-2
ANTOINE_GZ_STATE_PARTITION_ENTER run_id=antoine-swarm-flight-test callsign=drone-2 unix_ms=10000 after_ms=5000 duration_ms=1000 source_sequence=20 simulation_time_ns=9000000000
ANTOINE_GZ_STALE_HOLD_ENTER callsign=drone-2 stale_limit_ms=250 behavior=zero-velocity-setpoint
ANTOINE_GZ_STATE_PARTITION_EXIT run_id=antoine-swarm-flight-test callsign=drone-2 unix_ms=11000 elapsed_ms=6000 dropped_messages=10 last_dropped_source_sequence=29 recovery_source_sequence=30 last_dropped_simulation_time_ns=9990000000 recovery_simulation_time_ns=10000000000
ANTOINE_GZ_STALE_HOLD_EXIT callsign=drone-2 stale_limit_ms=250 behavior=zero-velocity-setpoint
"""
        (self.run_dir / "logs" / "antoine-controller-drone-2.log").write_text(controller)
        events = [
            {
                "runId": self.run_id,
                "callsign": "drone-2",
                "atUnixMs": 10500,
                "eventSequence": 7,
                "event": "grant_issued",
                "waypointIndex": 0,
                "grantSequence": 1,
                "setpointSequence": 1,
                "vigieAdmissionSequence": 1,
            }
        ]
        evidence = {"runId": self.run_id, "callsign": "drone-2", "missionProgress": events}
        (self.run_dir / "flight-evidence-drone-2.json").write_text(json.dumps(evidence))
        frames = []
        for sim_time in (8.75, 9.99, 10.0, 10.25):
            frames.append(
                {
                    "header": {"stamp": {"sec": int(sim_time), "nsec": int((sim_time % 1) * 1e9)}},
                    "pose": [
                        {"name": "model-a", "position": {"x": 0, "y": 0, "z": 1}},
                        {"name": "model-b", "position": {"x": 8, "y": 0, "z": 1}},
                    ],
                }
            )
        (self.run_dir / "gazebo-pose.jsonl").write_text(
            "\n".join(json.dumps(row) for row in frames)
        )
        rows = []
        for callsign in ("drone-1", "drone-2"):
            for sequence, timestamp in enumerate((9900, 10500, 11100), 1):
                rows.append(
                    {
                        "vehicleId": callsign,
                        "runId": self.run_id,
                        "recordedAtUnixMs": timestamp,
                        "sequence": sequence,
                        "values": {"velocity": [float(sequence), 0.0, 0.0, 0.0]},
                        "authority": "verified-core-grant",
                        "neutral": False,
                    }
                )
        (self.run_dir / "applier-neutral" / "actuator-output.jsonl").write_text(
            "\n".join(json.dumps(row) for row in rows)
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_pass_requires_recovery_and_authority_progress_inside_partition(self) -> None:
        report = summarize(self.run_dir)
        self.assertEqual(report["status"], "passed")
        self.assertTrue(report["markers"]["authoritySourceContinued"])
        self.assertEqual(report["markers"]["authorityProgress"]["eventSequences"], [7])
        self.assertEqual(
            report["actuatorWindows"]["drone-2"]["duringPartition"]["deltaFromBeforeL2"],
            1.0,
        )
        self.assertEqual(
            report["stateCoalescing"],
            {
                "status": "measured",
                "callsign": "drone-2",
                "enabledMarkerCount": 1,
                "eventCount": 0,
                "droppedSupersededMessages": 0,
                "events": [],
                "qualifiesFlightOutcome": False,
            },
        )

    def test_unsupported_fault_is_not_relabelled_as_a_state_partition(self) -> None:
        config_path = self.run_dir / "research-swarm-config.json"
        config = json.loads(config_path.read_text())
        config["fault"] = "command-rate"
        config_path.write_text(json.dumps(config))

        with self.assertRaisesRegex(ValueError, "unsupported or missing"):
            summarize(self.run_dir)

    def test_missing_fault_target_is_not_defaulted_to_another_vehicle(self) -> None:
        config_path = self.run_dir / "research-swarm-config.json"
        config = json.loads(config_path.read_text())
        del config["faultCallsign"]
        config_path.write_text(json.dumps(config))

        with self.assertRaisesRegex(ValueError, "target is missing"):
            summarize(self.run_dir)

    def test_missing_controller_log_is_reported_inconclusive(self) -> None:
        (self.run_dir / "logs" / "antoine-controller-drone-2.log").unlink()

        report = summarize(self.run_dir)

        self.assertEqual(report["status"], "inconclusive")
        self.assertEqual(report["partition"]["invalidReason"], "controller-log-missing")
        self.assertEqual(report["stateCoalescing"]["status"], "unavailable")

    def test_duplicate_partition_markers_are_reported_inconclusive(self) -> None:
        path = self.run_dir / "logs" / "antoine-controller-drone-2.log"
        lines = path.read_text().splitlines()
        lines.append(lines[1])
        path.write_text("\n".join(lines) + "\n")

        report = summarize(self.run_dir)

        self.assertEqual(report["status"], "inconclusive")
        self.assertEqual(
            report["partition"]["invalidReason"], "partition-markers-missing-or-ambiguous"
        )
        self.assertTrue(report["markers"]["partitionEntered"])

    def test_mismatched_partition_identity_is_reported_inconclusive(self) -> None:
        path = self.run_dir / "logs" / "antoine-controller-drone-2.log"
        path.write_text(path.read_text().replace(self.run_id, "foreign-run"))

        report = summarize(self.run_dir)

        self.assertEqual(report["status"], "inconclusive")
        self.assertEqual(
            report["partition"]["invalidReason"], "partition-marker-run-or-callsign-mismatch"
        )

    def test_malformed_partition_numbers_are_reported_inconclusive(self) -> None:
        path = self.run_dir / "logs" / "antoine-controller-drone-2.log"
        path.write_text(path.read_text().replace("dropped_messages=10", "dropped_messages=ten"))

        report = summarize(self.run_dir)

        self.assertEqual(report["status"], "inconclusive")
        self.assertEqual(
            report["partition"]["invalidReason"], "partition-marker-numeric-fields-invalid"
        )

    def test_actuator_vector_rejects_partial_or_empty_fields(self) -> None:
        self.assertIsNone(actuator_vector({"values": {"motors": [0.1, 0.2], "metadata": "bad"}}))
        self.assertIsNone(actuator_vector({"values": {"motors": [], "throttle": 0.4}}))
        self.assertIsNone(actuator_vector({"values": {"motors": [0.1, True]}}))

    def test_partial_actuator_vector_makes_partition_inconclusive(self) -> None:
        path = self.run_dir / "applier-neutral" / "actuator-output.jsonl"
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        for row in rows:
            if row["vehicleId"] == "drone-2" and row["recordedAtUnixMs"] == 10500:
                row["values"]["unknownField"] = "malformed"
        path.write_text("\n".join(json.dumps(row) for row in rows))

        report = summarize(self.run_dir)

        self.assertEqual(report["status"], "inconclusive")
        self.assertFalse(report["actuatorWindows"]["drone-2"]["identitySequenceAndValuesValid"])

    def test_state_coalescing_counts_target_events_without_changing_fault_status(self) -> None:
        path = self.run_dir / "logs" / "antoine-controller-drone-2.log"
        path.write_text(
            path.read_text()
            + "ANTOINE_GZ_STATE_COALESCED callsign=drone-2 dropped_superseded=4\n"
            + "ANTOINE_GZ_STATE_COALESCED callsign=drone-2 dropped_superseded=3\n"
            + "ANTOINE_GZ_STATE_COALESCED callsign=drone-1 dropped_superseded=9\n"
        )

        report = summarize(self.run_dir)

        self.assertEqual(report["status"], "passed")
        self.assertEqual(report["stateCoalescing"]["status"], "measured")
        self.assertEqual(report["stateCoalescing"]["callsign"], "drone-2")
        self.assertEqual(report["stateCoalescing"]["eventCount"], 2)
        self.assertEqual(report["stateCoalescing"]["droppedSupersededMessages"], 7)
        self.assertFalse(report["stateCoalescing"]["qualifiesFlightOutcome"])

    def test_malformed_state_coalescing_marker_is_reported_invalid(self) -> None:
        path = self.run_dir / "logs" / "antoine-controller-drone-2.log"
        path.write_text(
            path.read_text()
            + "ANTOINE_GZ_STATE_COALESCED callsign=drone-2 dropped_superseded=zero\n"
        )

        report = summarize(self.run_dir)

        self.assertEqual(report["status"], "passed")
        self.assertEqual(report["stateCoalescing"]["status"], "invalid")
        self.assertEqual(report["stateCoalescing"]["eventCount"], 0)

    def test_authority_progress_outside_partition_is_inconclusive(self) -> None:
        evidence_path = self.run_dir / "flight-evidence-drone-2.json"
        evidence = json.loads(evidence_path.read_text())
        evidence["missionProgress"][0]["atUnixMs"] = 12000
        evidence_path.write_text(json.dumps(evidence))
        report = summarize(self.run_dir)
        self.assertEqual(report["status"], "inconclusive")
        self.assertFalse(report["markers"]["authoritySourceContinued"])

    def test_malformed_authority_event_does_not_qualify_progress(self) -> None:
        evidence_path = self.run_dir / "flight-evidence-drone-2.json"
        evidence = json.loads(evidence_path.read_text())
        evidence["missionProgress"][0]["event"] = "unrecognized"
        evidence_path.write_text(json.dumps(evidence))

        report = summarize(self.run_dir)

        self.assertEqual(report["status"], "inconclusive")
        self.assertFalse(report["markers"]["authoritySourceContinued"])
        self.assertEqual(
            report["markers"]["authorityProgress"]["invalidReason"],
            "mission-progress-identity-sequence-or-binding-invalid",
        )

    def test_authority_progress_requires_all_three_sequence_bindings(self) -> None:
        evidence_path = self.run_dir / "flight-evidence-drone-2.json"
        evidence = json.loads(evidence_path.read_text())
        del evidence["missionProgress"][0]["vigieAdmissionSequence"]
        evidence_path.write_text(json.dumps(evidence))

        report = summarize(self.run_dir)

        self.assertEqual(report["status"], "inconclusive")
        self.assertFalse(report["markers"]["authoritySourceContinued"])

    def test_watchdog_markers_outside_the_partition_do_not_qualify(self) -> None:
        log_path = self.run_dir / "logs" / "antoine-controller-drone-2.log"
        lines = log_path.read_text().splitlines()
        hold_enter = next(line for line in lines if "STALE_HOLD_ENTER" in line)
        lines.remove(hold_enter)
        lines.insert(0, hold_enter)
        log_path.write_text("\n".join(lines) + "\n")

        report = summarize(self.run_dir)

        self.assertEqual(report["status"], "inconclusive")
        self.assertFalse(report["markers"]["watchdogOrderValid"])

    def test_actuator_during_sample_must_be_inside_the_partition(self) -> None:
        path = self.run_dir / "applier-neutral" / "actuator-output.jsonl"
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        for row in rows:
            if row["recordedAtUnixMs"] == 10500:
                row["recordedAtUnixMs"] = 9999
        path.write_text("\n".join(json.dumps(row) for row in rows))

        report = summarize(self.run_dir)

        self.assertEqual(report["status"], "inconclusive")
        self.assertFalse(report["actuatorWindows"]["drone-2"]["phaseTimestampsValid"])

    def test_pose_during_sample_must_precede_recovery(self) -> None:
        path = self.run_dir / "gazebo-pose.jsonl"
        frames = [json.loads(line) for line in path.read_text().splitlines()]
        frame = next(row for row in frames if row["header"]["stamp"]["sec"] == 9)
        frame["header"]["stamp"]["sec"] = 10
        frame["header"]["stamp"]["nsec"] = 10_010_000
        path.write_text("\n".join(json.dumps(row) for row in frames))

        report = summarize(self.run_dir)

        self.assertEqual(report["status"], "inconclusive")
        self.assertFalse(report["vehicles"]["drone-2"]["phaseTimestampsValid"])


if __name__ == "__main__":
    unittest.main()
