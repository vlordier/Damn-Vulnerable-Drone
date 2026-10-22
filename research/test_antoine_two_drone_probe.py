from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import antoine_two_drone_probe


class TwoDroneProbeTests(unittest.TestCase):
    def test_rejects_external_applier_log_before_subscribing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run_dir = root / "run"
            run_dir.mkdir()
            (run_dir / "logs").mkdir()
            (run_dir / "logs/nats.log").write_text(
                "Listening for client connections on 127.0.0.1:4222\nServer is ready\n"
            )
            external_log = root / "applier.log"
            external_log.write_text("startup-neutral-zero-published drone-1\n")
            with (
                patch(
                    "sys.argv",
                    [
                        "probe",
                        "--server",
                        "nats://127.0.0.1:4222",
                        "--run-dir",
                        str(run_dir),
                        "--applier-log",
                        str(external_log),
                    ],
                ),
                patch.object(antoine_two_drone_probe.subprocess, "run") as run,
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
            (run_dir / "actual-applier.log").write_text("startup-neutral-zero-published drone-1\n")
            (run_dir / "applier.log").symlink_to(run_dir / "actual-applier.log")
            with (
                patch(
                    "sys.argv",
                    [
                        "probe",
                        "--server",
                        "nats://127.0.0.1:4222",
                        "--run-dir",
                        str(run_dir),
                        "--applier-log",
                        str(run_dir / "applier.log"),
                    ],
                ),
                patch.object(antoine_two_drone_probe.subprocess, "run") as run,
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
            applier = run_dir / "applier.log"
            applier.write_text(
                "startup-neutral-zero-published drone-1\nstartup-neutral-zero-published drone-2\n"
            )
            state = json.dumps({"identity": "drone-1", "position_enu_m": [0, 0, 0]})
            other_state = json.dumps({"identity": "drone-2", "position_enu_m": [8, 0, 0]})
            with (
                patch(
                    "sys.argv",
                    [
                        "probe",
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
                    ],
                ),
            ):
                self.assertEqual(antoine_two_drone_probe.main(), 0)
            report = json.loads((run_dir / "research-two-drone-smoke.json").read_text())
            self.assertEqual(report["status"], "passed")


if __name__ == "__main__":
    unittest.main()
