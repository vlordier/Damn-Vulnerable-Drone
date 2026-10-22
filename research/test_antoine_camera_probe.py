from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import antoine_camera_probe


class AntoineCameraProbeTests(unittest.TestCase):
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
