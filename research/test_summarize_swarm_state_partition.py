from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from summarize_swarm_state_partition import summarize


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
        controller = """ANTOINE_GZ_STATE_PARTITION_ENTER run_id=antoine-swarm-flight-test callsign=drone-2 unix_ms=10000 after_ms=5000 duration_ms=1000 source_sequence=20 simulation_time_ns=9000000000
ANTOINE_GZ_STATE_PARTITION_EXIT run_id=antoine-swarm-flight-test callsign=drone-2 unix_ms=11000 elapsed_ms=6000 dropped_messages=10 last_dropped_source_sequence=29 recovery_source_sequence=30 last_dropped_simulation_time_ns=9990000000 recovery_simulation_time_ns=10000000000
ANTOINE_GZ_STALE_HOLD_ENTER callsign=drone-2 stale_limit_ms=250 behavior=zero-velocity-setpoint
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
            }
        ]
        evidence = {"runId": self.run_id, "callsign": "drone-2", "missionProgress": events}
        (self.run_dir / "flight-evidence-drone-2.json").write_text(json.dumps(evidence))
        frames = []
        for sim_time in (8.75, 9.99, 10.25):
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

    def test_authority_progress_outside_partition_is_inconclusive(self) -> None:
        evidence_path = self.run_dir / "flight-evidence-drone-2.json"
        evidence = json.loads(evidence_path.read_text())
        evidence["missionProgress"][0]["atUnixMs"] = 12000
        evidence_path.write_text(json.dumps(evidence))
        report = summarize(self.run_dir)
        self.assertEqual(report["status"], "inconclusive")
        self.assertFalse(report["markers"]["authoritySourceContinued"])


if __name__ == "__main__":
    unittest.main()
