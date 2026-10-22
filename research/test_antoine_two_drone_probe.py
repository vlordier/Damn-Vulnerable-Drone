from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import antoine_two_drone_probe


class TwoDroneProbeTests(unittest.TestCase):
    @staticmethod
    def state(callsign: str, model: str, position: list[float]) -> dict:
        return {
            "schema": "furia.gzsitl.state",
            "version": "1.1.0",
            "identity": callsign,
            "source": f"gz-sitl:{model}",
            "simulation_time_ns": 1_000_000_000,
            "position_enu_m": position,
        }

    def test_nats_state_must_match_callsign_model_and_finite_position(self) -> None:
        valid = self.state("drone-1", "isr", [0.0, 1.0, 2.0])
        self.assertEqual(
            antoine_two_drone_probe.validate_nats_state("drone-1", "isr", valid),
            (True, None),
        )
        wrong_model = {**valid, "source": "gz-sitl:fpv-a"}
        self.assertFalse(
            antoine_two_drone_probe.validate_nats_state("drone-1", "isr", wrong_model)[0]
        )
        for invalid_position in ([0.0, float("nan"), 2.0], [0.0, True, 2.0], [0.0, "1", 2.0]):
            with self.subTest(position=invalid_position):
                invalid = {**valid, "position_enu_m": invalid_position}
                self.assertFalse(
                    antoine_two_drone_probe.validate_nats_state("drone-1", "isr", invalid)[0]
                )
        invalid_time = {**valid, "simulation_time_ns": True}
        self.assertFalse(
            antoine_two_drone_probe.validate_nats_state("drone-1", "isr", invalid_time)[0]
        )
        self.assertFalse(
            antoine_two_drone_probe.validate_nats_state("drone-1", "isr", [1, 2, 3])[0]
        )
        out_of_range_time = {**valid, "simulation_time_ns": 1 << 64}
        self.assertFalse(
            antoine_two_drone_probe.validate_nats_state("drone-1", "isr", out_of_range_time)[0]
        )

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

    def test_gazebo_truth_rejects_duplicate_nested_json_keys(self) -> None:
        frame = (
            '{"header":{"stamp":{"sec":1,"nsec":0}},'
            '"pose":[{"name":"isr","position":{"x":0,"y":0,"z":0}},'
            '{"name":"fpv-a","position":{"x":8,"y":0,"z":0}}],'
            '"header":{"stamp":{"sec":2,"nsec":0}}}'
        )
        self.assertEqual(antoine_two_drone_probe.gazebo_pose_samples(frame), [])

    def test_pass_requires_correlated_separation_in_nats_and_gazebo(self) -> None:
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
                json.dumps(self.state("drone-1", "isr", [100, 0, 0])),
                json.dumps(self.state("drone-2", "fpv-a", [108, 0, 0])),
            ]
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
            self.assertTrue(report["gazeboTruth"]["separationAtLeast5m"])
            self.assertEqual(report["natsGazeboCorrelation"]["status"], "inconclusive")
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
            state = json.dumps(self.state("drone-1", "isr", [0, 0, 0]))
            other_state = json.dumps(self.state("drone-2", "fpv-a", [8, 0, 0]))
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
            self.assertEqual(report["natsGazeboCorrelation"]["status"], "matched")
            self.assertEqual(report["gazeboTruth"]["separationM"], 8.0)
            self.assertEqual(report["gazeboTruth"]["sampleCount"], 2)

    def test_missing_nats_and_gazebo_binaries_write_inconclusive_report(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory) / "run"
            run_dir.mkdir()
            applier = run_dir / "applier.log"
            applier.write_text(
                "startup-neutral-zero-published drone-1\nstartup-neutral-zero-published drone-2\n"
            )
            (run_dir / "research-profile.env").write_text(
                "GZ_IP=127.0.0.1\nGZ_PARTITION=test-partition\n"
            )
            with (
                patch(
                    "sys.argv",
                    [
                        "probe",
                        "--gz",
                        "/missing/gz",
                        "--server",
                        "nats://127.0.0.1:4222",
                        "--run-dir",
                        str(run_dir),
                        "--applier-log",
                        str(applier),
                    ],
                ),
                patch.object(
                    antoine_two_drone_probe,
                    "validate_run_nats_endpoint",
                    return_value=4222,
                ),
                patch.object(
                    antoine_two_drone_probe.subprocess,
                    "run",
                    side_effect=[
                        FileNotFoundError("nats"),
                        FileNotFoundError("nats"),
                        FileNotFoundError("/missing/gz"),
                    ],
                ),
                patch.dict(os.environ, {"GZ_IP": "127.0.0.1", "GZ_PARTITION": "test-partition"}),
            ):
                self.assertEqual(antoine_two_drone_probe.main(), 2)

            report = json.loads((run_dir / "research-two-drone-smoke.json").read_text())
            self.assertEqual(report["status"], "inconclusive")
            self.assertEqual(report["gazeboTruth"]["status"], "unavailable")
            self.assertIn("missing/gz", report["gazeboTruth"]["reason"])
            for state in report["vehicles"].values():
                self.assertFalse(state["received"])
                self.assertIn("NATS subscriber could not start", state["reason"])


if __name__ == "__main__":
    unittest.main()
