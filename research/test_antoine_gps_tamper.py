from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from antoine_gps_tamper import ACCEPTED_MARKER, count_accepted, read_log


class GpsTamperEvidenceTests(unittest.TestCase):
    def test_controller_acceptance_count_reads_the_run_owned_log(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory)
            logs = run_dir / "logs"
            logs.mkdir()
            (logs / "antoine-controller.log").write_text(
                f"startup\n{ACCEPTED_MARKER}\n{ACCEPTED_MARKER}\n", encoding="utf-8"
            )
            self.assertEqual(count_accepted(run_dir), 2)

    def test_log_reader_rejects_symlinked_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory) / "run"
            run_dir.mkdir()
            external = Path(directory) / "external.log"
            external.write_text(ACCEPTED_MARKER, encoding="utf-8")
            (run_dir / "controller.log").symlink_to(external)
            with self.assertRaisesRegex(ValueError, "symlink"):
                read_log(run_dir, Path("controller.log"))

    def test_missing_log_is_pending(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(read_log(Path(directory), Path("logs/controller.log")), "")


if __name__ == "__main__":
    unittest.main()
