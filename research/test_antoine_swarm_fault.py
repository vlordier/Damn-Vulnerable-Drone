from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import antoine_swarm_fault


class AntoineSwarmFaultTests(unittest.TestCase):
    def test_state_observer_rejects_duplicate_identity(self) -> None:
        self.assertIsNone(
            antoine_swarm_fault.matching_vehicle_state(
                b'{"identity":"spoofed","identity":"drone-2"}', "drone-2"
            )
        )
        self.assertEqual(
            antoine_swarm_fault.matching_vehicle_state(
                b'{"identity":"drone-2","time_s":1.0}', "drone-2"
            ),
            {"identity": "drone-2", "time_s": 1.0},
        )

    def test_missing_nats_subscriber_writes_inconclusive_report(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory) / "run"
            run_dir.mkdir()
            with (
                patch(
                    "sys.argv",
                    [
                        "observer",
                        "--server",
                        "nats://127.0.0.1:4222",
                        "--run-dir",
                        str(run_dir),
                        "--fault",
                        "state-stream-timing",
                        "--duration",
                        "0.25",
                        "--startup-timeout",
                        "0.25",
                        "--max-messages",
                        "1",
                    ],
                ),
                patch.object(
                    antoine_swarm_fault,
                    "validate_run_nats_endpoint",
                    return_value=4222,
                ),
                patch.object(
                    antoine_swarm_fault.subprocess,
                    "Popen",
                    side_effect=FileNotFoundError("nats"),
                ) as popen,
            ):
                self.assertEqual(antoine_swarm_fault.main(), 2)

            popen.assert_called_once()
            report = json.loads((run_dir / "research-swarm-fault.json").read_text())
            self.assertEqual(report["status"], "inconclusive")
            self.assertEqual(report["observedMessages"], 0)
            self.assertEqual(
                report["subscriberStderr"], "subscriber could not start: FileNotFoundError"
            )


if __name__ == "__main__":
    unittest.main()
