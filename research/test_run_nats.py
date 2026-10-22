from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from dvd_research.run_nats import read_run_nats_log, validate_run_nats_endpoint


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

    def test_run_endpoint_requires_its_exact_ready_loopback_port(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory) / "run-a"
            log = run_dir / "logs" / "nats.log"
            log.parent.mkdir(parents=True)
            log.write_text(
                "Listening for client connections on 127.0.0.1:4222\nServer is ready\n",
                encoding="utf-8",
            )

            self.assertEqual(
                validate_run_nats_endpoint("nats://127.0.0.1:4222", run_dir, time.monotonic() + 1),
                4222,
            )
            with self.assertRaisesRegex(TimeoutError, "did not confirm"):
                validate_run_nats_endpoint("nats://127.0.0.1:4223", run_dir, time.monotonic() - 1)

    def test_run_endpoint_rejects_noncanonical_or_nonloopback_urls(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory)
            invalid_urls = (
                "nats://localhost:4222",
                "nats://127.0.0.2:4222",
                "nats://user@127.0.0.1:4222",
                "nats://127.0.0.1:0",
                "nats://127.0.0.1:65536",
                "nats://127.0.0.1:4222/other",
                "nats://127.0.0.1:4222?token=secret",
            )
            for server in invalid_urls:
                with self.subTest(server=server), self.assertRaisesRegex(ValueError, "endpoint"):
                    validate_run_nats_endpoint(server, run_dir, time.monotonic() + 1)

    def test_run_endpoint_rejects_an_external_readiness_log(self) -> None:
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

            with self.assertRaisesRegex(ValueError, "contains a symlink"):
                validate_run_nats_endpoint("nats://127.0.0.1:4222", run_dir, time.monotonic() + 1)
