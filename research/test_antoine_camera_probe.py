from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import antoine_camera_probe


class AntoineCameraProbeTests(unittest.TestCase):
    def test_partial_stdout_from_failed_subscriber_is_not_camera_evidence(self) -> None:
        topic = "/world/antoine_gz_flight_proof/model/isr/link/base_link/sensor/camera/image"
        discovery = type("Completed", (), {"returncode": 0, "stdout": f"{topic}\n", "stderr": ""})()
        publisher = type(
            "Completed", (), {"returncode": 0, "stdout": "Publisher count: 1", "stderr": ""}
        )()
        capture = type(
            "Completed",
            (),
            {
                "returncode": 1,
                "stdout": '{"width":1,"height":1,"data":[0]}\n',
                "stderr": "subscriber failed",
            },
        )()
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory) / "run"
            run_dir.mkdir()
            (run_dir / "research-profile.env").write_text(
                "GZ_IP=127.0.0.1\nGZ_PARTITION=test-partition\n"
            )
            with (
                patch(
                    "sys.argv",
                    ["camera-probe", "--gz", "gz", "--topic", topic, "--run-dir", str(run_dir)],
                ),
                patch.dict("os.environ", {"GZ_IP": "127.0.0.1", "GZ_PARTITION": "test-partition"}),
                patch.object(
                    antoine_camera_probe.subprocess,
                    "run",
                    side_effect=[discovery, publisher, capture],
                ),
            ):
                self.assertEqual(antoine_camera_probe.main(), 2)

            report = json.loads((run_dir / "research-camera-observation.json").read_text())
            self.assertEqual(report["validImageFrames"], 1)
            self.assertFalse(report["captureValid"])
            self.assertIn("inconclusive", report["interpretation"])

    def test_camera_stream_rejects_duplicate_dimensions_and_non_finite_json(self) -> None:
        duplicate = '{"width":1,"height":1,"data":[0],"width":99}'
        non_finite = '{"width":1,"height":1,"data":[NaN]}'
        nested_duplicate = (
            '{"width":1,"height":1,"data":[0],"nested":'
            '{"width":2,"height":2,"data":[0]},"width":99}'
        )

        for output in (duplicate, non_finite, nested_duplicate):
            with self.subTest(output=output):
                self.assertEqual(
                    antoine_camera_probe.parse_camera_messages(output), (0, 0, 0, set(), set())
                )

    def test_missing_gazebo_cli_writes_inconclusive_report_without_retrying(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory) / "run"
            run_dir.mkdir()
            (run_dir / "research-profile.env").write_text(
                "GZ_IP=127.0.0.1\nGZ_PARTITION=test-partition\n"
            )
            with (
                patch(
                    "sys.argv",
                    [
                        "camera-probe",
                        "--gz",
                        "/missing/gz",
                        "--topic",
                        "/world/antoine_gz_flight_proof/model/isr/link/base_link/sensor/camera/image",
                        "--run-dir",
                        str(run_dir),
                    ],
                ),
                patch.object(
                    antoine_camera_probe.subprocess,
                    "run",
                    side_effect=[
                        FileNotFoundError("gz"),
                        FileNotFoundError("gz"),
                    ],
                ) as run,
                patch.dict(
                    "os.environ",
                    {"GZ_IP": "127.0.0.1", "GZ_PARTITION": "test-partition"},
                ),
            ):
                self.assertEqual(antoine_camera_probe.main(), 2)

            self.assertEqual(run.call_count, 2)
            report = json.loads((run_dir / "research-camera-observation.json").read_text())
            self.assertEqual(report["capturedMessages"], 0)
            self.assertEqual(report["discoveryError"], "FileNotFoundError")
            self.assertEqual(report["subscriberStartError"], "FileNotFoundError")
            self.assertFalse(report["subscriberTimedOut"])

    def test_camera_probe_requires_its_recorded_loopback_partition(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory) / "run"
            run_dir.mkdir()
            (run_dir / "research-profile.env").write_text(
                "GZ_IP=127.0.0.1\nGZ_PARTITION=run-partition\n"
            )
            self.assertEqual(
                antoine_camera_probe.validate_camera_scope(
                    run_dir, {"GZ_IP": "127.0.0.1", "GZ_PARTITION": "run-partition"}
                ),
                "run-partition",
            )
            for env in (
                {"GZ_IP": "127.0.0.1", "GZ_PARTITION": "other-partition"},
                {"GZ_IP": "192.0.2.5", "GZ_PARTITION": "run-partition"},
            ):
                with self.subTest(env=env), self.assertRaises(ValueError):
                    antoine_camera_probe.validate_camera_scope(run_dir, env)


if __name__ == "__main__":
    unittest.main()
