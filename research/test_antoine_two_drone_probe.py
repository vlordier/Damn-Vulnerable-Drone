from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import antoine_two_drone_probe


class TwoDroneProbeTests(unittest.TestCase):
    def test_gazebo_truth_requires_advancing_complete_pose_frames(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory)
            frame = {
                "header": {"stamp": {"sec": 2, "nsec": 0}},
                "pose": [
                    {"name": "isr", "position": {"x": 0, "y": 0, "z": 0}},
                    {"name": "fpv-a", "position": {"x": 8, "y": 0, "z": 0}},
                ],
            }
            payload = f"{json.dumps(frame)}\n{json.dumps(frame)}"
            with patch.object(
                antoine_two_drone_probe.subprocess,
                "run",
                return_value=type(
                    "Completed", (), {"returncode": 0, "stdout": payload, "stderr": ""}
                )(),
            ):
                observation = antoine_two_drone_probe.capture_gazebo_truth(
                    "/fake/gz", run_dir, "test-partition"
                )
            self.assertEqual(observation["status"], "inconclusive")
            self.assertTrue((run_dir / "gazebo-pose-info.jsonl").is_file())

    def test_pass_requires_separation_in_both_nats_and_gazebo(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory) / "run"
            (run_dir / "logs").mkdir(parents=True)
            (run_dir / "logs/nats.log").write_text(
                "Listening for client connections on 127.0.0.1:4222\nServer is ready\n"
            )
            (run_dir / "research-profile.env").write_text(
                "GZ_IP=127.0.0.1\nGZ_PARTITION=test-partition\n"
            )
            applier = run_dir / "applier.log"
            applier.write_text(
                "startup-neutral-zero-published drone-1\nstartup-neutral-zero-published drone-2\n"
            )
            states = [
                json.dumps({"identity": "drone-1", "position_enu_m": [0, 0, 0]}),
                json.dumps({"identity": "drone-2", "position_enu_m": [8, 0, 0]}),
            ]
            gazebo_samples = "\n".join(
                json.dumps(
                    {
                        "header": {"stamp": {"sec": second, "nsec": 0}},
                        "pose": [
                            {"name": "isr", "position": {"x": 0, "y": 0, "z": 0}},
                            {"name": "fpv-a", "position": {"x": 2, "y": 0, "z": 0}},
                        ],
                    }
                )
                for second in (1, 2)
            )
            completed = [
                type("Completed", (), {"returncode": 0, "stdout": state, "stderr": ""})()
                for state in states
            ]
            completed.append(
                type("Completed", (), {"returncode": 0, "stdout": gazebo_samples, "stderr": ""})()
            )
            with (
                patch(
                    "sys.argv",
                    [
                        "probe",
                        "--gz",
                        "/fake/gz",
                        "--server",
                        "nats://127.0.0.1:4222",
                        "--run-dir",
                        str(run_dir),
                        "--applier-log",
                        str(applier),
                    ],
                ),
                patch.object(antoine_two_drone_probe.subprocess, "run", side_effect=completed),
                patch.dict(os.environ, {"GZ_IP": "127.0.0.1", "GZ_PARTITION": "test-partition"}),
            ):
                self.assertEqual(antoine_two_drone_probe.main(), 2)
            report = json.loads((run_dir / "research-two-drone-smoke.json").read_text())
            self.assertTrue(report["natsStateSeparationAtLeast5m"])
            self.assertFalse(report["gazeboTruth"]["separationAtLeast5m"])
            self.assertFalse(report["initialSeparationAtLeast5m"])

    def test_rejects_external_applier_log_before_subscribing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run_dir = root / "run"
            run_dir.mkdir()
            (run_dir / "logs").mkdir()
            (run_dir / "logs/nats.log").write_text(
                "Listening for client connections on 127.0.0.1:4222\nServer is ready\n"
            )
            (run_dir / "research-profile.env").write_text(
                "GZ_IP=127.0.0.1\nGZ_PARTITION=test-partition\n"
            )
            external_log = root / "applier.log"
            external_log.write_text("startup-neutral-zero-published drone-1\n")
            with (
                patch(
                    "sys.argv",
                    [
                        "probe",
                        "--gz",
                        "/fake/gz",
                        "--server",
                        "nats://127.0.0.1:4222",
                        "--run-dir",
                        str(run_dir),
                        "--applier-log",
                        str(external_log),
                    ],
                ),
                patch.object(antoine_two_drone_probe.subprocess, "run") as run,
                patch.dict(os.environ, {"GZ_IP": "127.0.0.1", "GZ_PARTITION": "test-partition"}),
            ):
                with self.assertRaises(SystemExit):
                    antoine_two_drone_probe.main()
            run.assert_not_called()

    def test_rejects_symlinked_applier_log_before_subscribing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory) / "run"
            (run_dir / "logs").mkdir(parents=True)
            (run_dir / "logs/nats.log").write_text(
                "Listening for client connections on 127.0.0.1:4222\nServer is ready\n"
            )
            (run_dir / "research-profile.env").write_text(
                "GZ_IP=127.0.0.1\nGZ_PARTITION=test-partition\n"
            )
            (run_dir / "actual-applier.log").write_text("startup-neutral-zero-published drone-1\n")
            (run_dir / "applier.log").symlink_to(run_dir / "actual-applier.log")
            with (
                patch(
                    "sys.argv",
                    [
                        "probe",
                        "--gz",
                        "/fake/gz",
                        "--server",
                        "nats://127.0.0.1:4222",
                        "--run-dir",
                        str(run_dir),
                        "--applier-log",
                        str(run_dir / "applier.log"),
                    ],
                ),
                patch.object(antoine_two_drone_probe.subprocess, "run") as run,
                patch.dict(os.environ, {"GZ_IP": "127.0.0.1", "GZ_PARTITION": "test-partition"}),
            ):
                with self.assertRaises(SystemExit):
                    antoine_two_drone_probe.main()
            run.assert_not_called()

    def test_accepts_only_run_owned_broker_and_logs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory) / "run"
            (run_dir / "logs").mkdir(parents=True)
            (run_dir / "logs/nats.log").write_text(
                "Listening for client connections on 127.0.0.1:4222\nServer is ready\n"
            )
            (run_dir / "research-profile.env").write_text(
                "GZ_IP=127.0.0.1\nGZ_PARTITION=test-partition\n"
            )
            applier = run_dir / "applier.log"
            applier.write_text(
                "startup-neutral-zero-published drone-1\nstartup-neutral-zero-published drone-2\n"
            )
            state = json.dumps({"identity": "drone-1", "position_enu_m": [0, 0, 0]})
            other_state = json.dumps({"identity": "drone-2", "position_enu_m": [8, 0, 0]})
            gazebo_samples = "\n".join(
                json.dumps(
                    {
                        "header": {"stamp": {"sec": second, "nsec": 0}},
                        "pose": [
                            {"name": "isr", "position": {"x": 0, "y": 0, "z": 0}},
                            {"name": "fpv-a", "position": {"x": 8, "y": 0, "z": 0}},
                        ],
                    }
                )
                for second in (1, 2)
            )
            with (
                patch(
                    "sys.argv",
                    [
                        "probe",
                        "--gz",
                        "/fake/gz",
                        "--server",
                        "nats://127.0.0.1:4222",
                        "--run-dir",
                        str(run_dir),
                        "--applier-log",
                        str(applier),
                    ],
                ),
                patch.object(
                    antoine_two_drone_probe.subprocess,
                    "run",
                    side_effect=[
                        type("Completed", (), {"returncode": 0, "stdout": state, "stderr": ""})(),
                        type(
                            "Completed", (), {"returncode": 0, "stdout": other_state, "stderr": ""}
                        )(),
                        type(
                            "Completed",
                            (),
                            {"returncode": 0, "stdout": gazebo_samples, "stderr": ""},
                        )(),
                    ],
                ),
                patch.dict(os.environ, {"GZ_IP": "127.0.0.1", "GZ_PARTITION": "test-partition"}),
            ):
                self.assertEqual(antoine_two_drone_probe.main(), 0)
            report = json.loads((run_dir / "research-two-drone-smoke.json").read_text())
            self.assertEqual(report["status"], "passed")
            self.assertEqual(report["gazeboTruth"]["separationM"], 8.0)
            self.assertEqual(report["gazeboTruth"]["sampleCount"], 2)


if __name__ == "__main__":
    unittest.main()
