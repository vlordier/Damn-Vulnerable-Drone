from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from antoine_setpoint_mutation import validate_server as validate_mutation_server
from antoine_setpoint_replay import validate_server as validate_replay_server
from dvd_research.run_nats import read_run_nats_log


class RunNatsEvidenceTests(unittest.TestCase):
    def test_missing_run_or_log_is_not_yet_ready(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory) / "run-a"

            self.assertIsNone(read_run_nats_log(run_dir))

            run_dir.mkdir()
            self.assertIsNone(read_run_nats_log(run_dir))

    def test_reads_regular_run_owned_readiness_log(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory) / "run-a"
            log = run_dir / "logs" / "nats.log"
            log.parent.mkdir(parents=True)
            log.write_text("Server is ready\n", encoding="utf-8")

            self.assertEqual(read_run_nats_log(run_dir), "Server is ready\n")

    def test_rejects_symlinked_log_parent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run_dir = root / "run-a"
            run_dir.mkdir()
            outside = root / "outside-logs"
            outside.mkdir()
            (outside / "nats.log").write_text("Server is ready\n", encoding="utf-8")
            (run_dir / "logs").symlink_to(outside, target_is_directory=True)

            with self.assertRaisesRegex(ValueError, "contains a symlink"):
                read_run_nats_log(run_dir)

    def test_rejects_symlinked_log_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run_dir = root / "run-a"
            logs = run_dir / "logs"
            logs.mkdir(parents=True)
            outside = root / "outside-nats.log"
            outside.write_text("Server is ready\n", encoding="utf-8")
            (logs / "nats.log").symlink_to(outside)

            with self.assertRaisesRegex(ValueError, "contains a symlink"):
                read_run_nats_log(run_dir)

    def test_rejects_symlinked_run_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run_dir = root / "run-a"
            run_dir.mkdir()
            alias = root / "run-alias"
            alias.symlink_to(run_dir, target_is_directory=True)

            with self.assertRaisesRegex(ValueError, "run directory is a symlink"):
                read_run_nats_log(alias)

    def test_mutation_and_replay_refuse_external_readiness_logs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run_dir = root / "run-a"
            run_dir.mkdir()
            outside = root / "outside-logs"
            outside.mkdir()
            (outside / "nats.log").write_text(
                "Listening for client connections on 127.0.0.1:4222\nServer is ready\n",
                encoding="utf-8",
            )
            (run_dir / "logs").symlink_to(outside, target_is_directory=True)
            deadline = time.monotonic() + 1

            for validate in (validate_mutation_server, validate_replay_server):
                with (
                    self.subTest(validate=validate.__module__),
                    self.assertRaisesRegex(ValueError, "contains a symlink"),
                ):
                    validate("nats://127.0.0.1:4222", run_dir, deadline)
