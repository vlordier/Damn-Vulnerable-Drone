from __future__ import annotations

import math
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from check_swarm_host_load import measure_load

ROOT = Path(__file__).resolve().parent.parent
SWARM_RUNNER = ROOT / "research/run-antoine-two-drone-flight.sh"


class SwarmHostLoadTests(unittest.TestCase):
    def test_accepts_load_below_the_fractional_cpu_cap(self) -> None:
        result = measure_load([3.0, 5.0, 40.0], logical_cpu_count=8, max_load_per_cpu=0.75)

        self.assertEqual(result["status"], "quiet")
        self.assertEqual(result["maximumAllowedLoad"], 6.0)

    def test_uses_the_higher_recent_load_average_and_rejects_overload(self) -> None:
        result = measure_load([2.0, 6.01, 100.0], logical_cpu_count=8, max_load_per_cpu=0.75)

        self.assertEqual(result["status"], "busy")
        self.assertEqual(result["measuredMaximumLoad"], 6.01)

    def test_includes_a_load_exactly_at_the_configured_cap(self) -> None:
        result = measure_load([6.0, 5.0], logical_cpu_count=8, max_load_per_cpu=0.75)

        self.assertEqual(result["status"], "quiet")

    def test_rejects_invalid_cpu_count_cap_and_load(self) -> None:
        cases = (
            ([1.0, 1.0], 0, 0.75),
            ([1.0, 1.0], 8, 0.0),
            ([1.0, 1.0], 8, True),
            ([1.0, 1.0], 8, math.inf),
            ([math.nan, 1.0], 8, 0.75),
            ([True, 1.0], 8, 0.75),
            ([1.0], 8, 0.75),
        )
        for loads, cpus, cap in cases:
            with self.subTest(loads=loads, cpus=cpus, cap=cap), self.assertRaises(ValueError):
                measure_load(loads, cpus, cap)

    def test_swarm_runner_fails_before_creating_run_artifacts_when_host_is_busy(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            reports = root / "reports"
            environment = os.environ.copy()
            environment.update(
                {
                    "ANTOINE_REPORT_DIR": str(reports),
                    "ANTOINE_GZ_CARGO_TARGET_DIR": str(root / "cargo-target"),
                    "ANTOINE_SWARM_MIN_FREE_MIB": "1",
                    "ANTOINE_SWARM_MAX_LOAD_PER_CPU": "0.000001",
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
            self.assertIn("Host load is too high", result.stderr)
            self.assertFalse(reports.exists())


if __name__ == "__main__":
    unittest.main()
