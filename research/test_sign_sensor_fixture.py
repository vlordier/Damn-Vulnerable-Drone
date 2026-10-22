from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from sign_sensor_fixture import verify_scope


class SensorFixtureScopeTests(unittest.TestCase):
    def test_accepts_only_a_matching_run_owned_nats_log(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory) / "antoine-gps-run-a"
            log = run_dir / "logs" / "nats.log"
            log.parent.mkdir(parents=True)
            log.write_text(
                "Listening for client connections on 127.0.0.1:14226\nServer is ready\n",
                encoding="utf-8",
            )

            verify_scope("nats://127.0.0.1:14226", run_dir)
            with self.assertRaisesRegex(ValueError, "not confirmed"):
                verify_scope("nats://127.0.0.1:14227", run_dir)

    def test_rejects_external_readiness_log_before_signing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run_dir = root / "antoine-gps-run-a"
            run_dir.mkdir()
            outside = root / "outside-logs"
            outside.mkdir()
            (outside / "nats.log").write_text(
                "Listening for client connections on 127.0.0.1:14226\nServer is ready\n",
                encoding="utf-8",
            )
            (run_dir / "logs").symlink_to(outside, target_is_directory=True)

            with self.assertRaisesRegex(ValueError, "contains a symlink"):
                verify_scope("nats://127.0.0.1:14226", run_dir)
