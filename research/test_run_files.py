"""Run-owned artifact reads and writes reject symlink escapes."""

from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from antoine_setpoint_mutation import (
    last_motor_sequence,
    read_audit_rows,
    wait_for_controller_ready,
)
from dvd_research.run_files import (
    read_run_tail_text,
    read_run_text,
    resolve_run_file,
    write_run_json,
)


class RunFilesTests(unittest.TestCase):
    def test_reads_regular_run_file_and_treats_missing_file_as_pending(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory) / "run"
            run_dir.mkdir()
            (run_dir / "logs").mkdir()
            (run_dir / "logs/controller.log").write_text("ready\n", encoding="utf-8")

            self.assertEqual(read_run_text(run_dir, Path("logs/controller.log")), "ready\n")
            self.assertIsNone(read_run_text(run_dir, Path("logs/not-ready.log")))

    def test_bounded_tail_discards_a_partial_first_record(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory) / "run"
            run_dir.mkdir()
            (run_dir / "stream.jsonl").write_text("first\nsecond\nthird\n", encoding="utf-8")

            self.assertEqual(read_run_tail_text(run_dir, Path("stream.jsonl"), 10), "third\n")

    def test_rejects_linked_run_root_parent_and_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run_dir = root / "run"
            run_dir.mkdir()
            outside = root / "outside"
            outside.mkdir()
            (outside / "controller.log").write_text("external\n", encoding="utf-8")
            (run_dir / "logs").symlink_to(outside)

            with self.assertRaisesRegex(ValueError, "symlink"):
                read_run_text(run_dir, Path("logs/controller.log"))

            (run_dir / "logs").unlink()
            (run_dir / "logs").mkdir()
            (run_dir / "logs/controller.log").symlink_to(outside / "controller.log")
            with self.assertRaisesRegex(ValueError, "symlink"):
                resolve_run_file(run_dir, Path("logs/controller.log"))

            linked_run = root / "linked-run"
            linked_run.symlink_to(run_dir)
            with self.assertRaisesRegex(ValueError, "run directory is a symlink"):
                read_run_text(linked_run, Path("logs/controller.log"))

    def test_rejects_absolute_and_traversing_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory) / "run"
            run_dir.mkdir()

            for relative in (Path("../outside"), Path("/tmp/outside"), Path(".")):
                with (
                    self.subTest(relative=relative),
                    self.assertRaisesRegex(ValueError, "relative"),
                ):
                    resolve_run_file(run_dir, relative)

    def test_json_output_is_atomic_and_does_not_follow_linked_destination(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run_dir = root / "run"
            run_dir.mkdir()
            destination = run_dir / "report.json"
            write_run_json(run_dir, Path("report.json"), {"status": "ok"})
            self.assertEqual(destination.read_text(encoding="utf-8"), '{\n  "status": "ok"\n}\n')
            self.assertEqual(list(run_dir.glob("*.tmp")), [])

            external = root / "external.json"
            external.write_text("preserve\n", encoding="utf-8")
            destination.unlink()
            destination.symlink_to(external)
            with self.assertRaisesRegex(ValueError, "destination is a symlink"):
                write_run_json(run_dir, Path("report.json"), {"status": "tampered"})
            self.assertEqual(external.read_text(encoding="utf-8"), "preserve\n")
            self.assertTrue(destination.is_symlink())

    def test_waypoint_probe_rejects_linked_controller_motor_and_audit_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run_dir = root / "run"
            for relative in (
                "logs",
                "applier-authorized",
                "applier-neutral",
            ):
                (run_dir / relative).mkdir(parents=True, exist_ok=True)
            outside = root / "outside"
            outside.mkdir()
            (outside / "controller.log").write_text(
                "ANTOINE_GZ_SETPOINT_RECEIVED callsign=drone-1\n", encoding="utf-8"
            )
            (outside / "motors").write_text("99\n", encoding="utf-8")
            (outside / "audit.jsonl").write_text(
                '{"stage":"explicit-stop","neutral":true}\n', encoding="utf-8"
            )
            (run_dir / "logs/antoine-controller.log").symlink_to(outside / "controller.log")
            (run_dir / "applier-authorized/drone-1.motors").symlink_to(outside / "motors")
            (run_dir / "applier-neutral/actuator-output.jsonl").symlink_to(outside / "audit.jsonl")

            with self.assertRaisesRegex(ValueError, "symlink"):
                wait_for_controller_ready(run_dir, time.monotonic() + 1)
            with self.assertRaisesRegex(ValueError, "symlink"):
                last_motor_sequence(run_dir)
            with self.assertRaisesRegex(ValueError, "symlink"):
                read_audit_rows(run_dir)


if __name__ == "__main__":
    unittest.main()
