from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import antoine_nats_observation


class AntoineNatsObservationTests(unittest.TestCase):
    def test_cli_start_failure_is_recorded_as_incomplete_evidence(self) -> None:
        ready = type("Completed", (), {"returncode": 0, "stdout": "", "stderr": ""})()
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory)
            with (
                patch(
                    "sys.argv",
                    [
                        "observe",
                        "--server",
                        "nats://127.0.0.1:4222",
                        "--run-dir",
                        str(run_dir),
                    ],
                ),
                patch.object(
                    antoine_nats_observation,
                    "validate_run_nats_endpoint",
                    return_value=4222,
                ),
                patch.object(
                    antoine_nats_observation.subprocess,
                    "run",
                    side_effect=[ready, *[FileNotFoundError("nats CLI missing")] * 4],
                ),
            ):
                result = antoine_nats_observation.main()

            self.assertEqual(result, 2)
            report = json.loads((run_dir / "research-nats-observation.json").read_text())
            self.assertTrue(all(item["capturedMessages"] == 0 for item in report["subjects"]))
            self.assertTrue(
                all(
                    item["subscriberStartError"] == "FileNotFoundError: nats CLI missing"
                    for item in report["subjects"]
                )
            )
            self.assertTrue(all(not item["subscriberTimedOut"] for item in report["subjects"]))


if __name__ == "__main__":
    unittest.main()
