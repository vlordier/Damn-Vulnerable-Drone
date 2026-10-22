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
            ):
                self.assertEqual(antoine_camera_probe.main(), 2)

            self.assertEqual(run.call_count, 2)
            report = json.loads((run_dir / "research-camera-observation.json").read_text())
            self.assertEqual(report["capturedMessages"], 0)
            self.assertEqual(report["discoveryError"], "FileNotFoundError")
            self.assertEqual(report["subscriberStartError"], "FileNotFoundError")
            self.assertFalse(report["subscriberTimedOut"])


if __name__ == "__main__":
    unittest.main()
