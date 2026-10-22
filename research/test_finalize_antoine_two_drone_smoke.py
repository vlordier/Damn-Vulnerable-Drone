from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from finalize_antoine_two_drone_smoke import finalize


class TwoDroneSmokeFinalizerTests(unittest.TestCase):
    def make_run(self, root: Path, status: str = "passed") -> Path:
        run_dir = root / "run"
        run_dir.mkdir()
        (run_dir / "research-two-drone-smoke.json").write_text(
            json.dumps({"status": status, "limits": "bounded smoke"}), encoding="utf-8"
        )
        return run_dir

    def test_stable_provenance_preserves_a_passed_smoke(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_dir = self.make_run(Path(directory))
            (run_dir / "research-provenance-comparison.json").write_text(
                json.dumps({"stable": True, "changedSourceTrees": [], "changedInputs": []}),
                encoding="utf-8",
            )
            report, status = finalize(run_dir, 0)
            self.assertEqual(status, 0)
            self.assertEqual(report["status"], "passed")
            self.assertTrue(report["provenanceStable"])

    def test_unstable_provenance_downgrades_a_passed_smoke(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_dir = self.make_run(Path(directory))
            (run_dir / "research-provenance-comparison.json").write_text(
                json.dumps({"stable": False, "changedInputs": ["gazebo_applier"]}),
                encoding="utf-8",
            )
            report, status = finalize(run_dir, 0)
            self.assertEqual(status, 2)
            self.assertEqual(report["status"], "inconclusive")
            self.assertEqual(report["provenance"]["changedInputs"], ["gazebo_applier"])

    def test_failed_capture_cannot_claim_stable_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_dir = self.make_run(Path(directory), status="inconclusive")
            (run_dir / "research-provenance-comparison.json").write_text(
                json.dumps({"stable": True}), encoding="utf-8"
            )
            report, status = finalize(run_dir, 1)
            self.assertEqual(status, 2)
            self.assertFalse(report["provenanceStable"])
            self.assertEqual(report["status"], "inconclusive")


if __name__ == "__main__":
    unittest.main()
