from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CHECK_FREE_SPACE = ROOT / "research/check-free-space.sh"
SWARM_RUNNER = ROOT / "research/run-antoine-two-drone-flight.sh"


class FreeSpacePreflightTests(unittest.TestCase):
    def run_check(self, minimum_mib: str) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        environment["MIN_FREE_MIB"] = minimum_mib
        environment.pop("QGC_GAZEBO_MIN_FREE_MIB", None)
        return subprocess.run(
            [str(CHECK_FREE_SPACE), str(ROOT)],
            capture_output=True,
            check=False,
            env=environment,
            text=True,
        )

    def test_accepts_available_space_above_the_configured_minimum(self) -> None:
        result = self.run_check("1")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("preflight passed", result.stdout)

    def test_rejects_insufficient_space_before_a_run_starts(self) -> None:
        result = self.run_check("999999")

        self.assertEqual(result.returncode, 1)
        self.assertIn("Insufficient free disk space", result.stderr)
        self.assertIn("set MIN_FREE_MIB", result.stderr)

    def test_rejects_invalid_minimum(self) -> None:
        result = self.run_check("0")

        self.assertEqual(result.returncode, 2)
        self.assertIn("MIN_FREE_MIB must be a positive integer", result.stderr)

    def test_swarm_runner_fails_before_creating_run_artifacts_when_space_is_low(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            reports = root / "reports"
            environment = os.environ.copy()
            environment.update(
                {
                    "ANTOINE_REPORT_DIR": str(reports),
                    "ANTOINE_GZ_CARGO_TARGET_DIR": str(root / "cargo-target"),
                    "ANTOINE_SWARM_MIN_FREE_MIB": "999999",
                    "ANTOINE_RESEARCH_FLIGHT_SECONDS": "20",
                }
            )
            result = subprocess.run(
                ["bash", str(SWARM_RUNNER)],
                capture_output=True,
                check=False,
                env=environment,
                text=True,
            )

            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertIn("Insufficient free disk space", result.stderr)
            self.assertFalse(reports.exists())


if __name__ == "__main__":
    unittest.main()
