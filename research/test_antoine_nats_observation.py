from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import antoine_nats_observation


class AntoineNatsObservationTests(unittest.TestCase):
    def test_capture_counts_duplicate_key_payload_as_malformed(self) -> None:
        completed = type(
            "Completed",
            (),
            {
                "returncode": 0,
                "stdout": '{"identity":"spoofed","identity":"drone-1"}\n',
                "stderr": "",
            },
        )()
        with patch.object(antoine_nats_observation.subprocess, "run", return_value=completed):
            result = antoine_nats_observation.capture(
                "nats://127.0.0.1:4222", "telemetry", "furia.telemetry.drone-1"
            )

        self.assertEqual(result["capturedMessages"], 0)
        self.assertEqual(result["malformedOutputLines"], 1)

    def test_subscriber_cli_start_failure_is_recorded_as_incomplete_evidence(self) -> None:
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

    def test_missing_readiness_cli_writes_report_without_subscriber_attempts(self) -> None:
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
                    side_effect=FileNotFoundError("nats"),
                ),
                patch.object(antoine_nats_observation, "capture") as capture,
            ):
                self.assertEqual(antoine_nats_observation.main(), 2)

            capture.assert_not_called()
            report = json.loads((run_dir / "research-nats-observation.json").read_text())
            self.assertEqual(
                report["readinessError"],
                "NATS readiness command could not start (FileNotFoundError)",
            )
            self.assertTrue(all(item["subscriberAttempts"] == 0 for item in report["subjects"]))

    def test_broker_not_ready_writes_report_instead_of_exiting(self) -> None:
        not_ready = type("Completed", (), {"returncode": 1, "stdout": "", "stderr": ""})()
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
                    return_value=not_ready,
                ),
                patch.object(antoine_nats_observation.time, "monotonic", side_effect=[0, 0, 1, 11]),
                patch.object(antoine_nats_observation.time, "sleep"),
                patch.object(antoine_nats_observation, "capture") as capture,
            ):
                self.assertEqual(antoine_nats_observation.main(), 2)

            capture.assert_not_called()
            report = json.loads((run_dir / "research-nats-observation.json").read_text())
            self.assertEqual(
                report["readinessError"], "the run-owned loopback NATS broker did not become ready"
            )
            self.assertTrue(
                all(
                    item["skippedReason"] == report["readinessError"] for item in report["subjects"]
                )
            )


if __name__ == "__main__":
    unittest.main()
