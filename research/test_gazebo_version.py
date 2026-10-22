from __future__ import annotations

import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from gazebo_version import gazebo_version


class GazeboVersionTests(unittest.TestCase):
    def test_uses_supported_sim_versions_command(self) -> None:
        with patch("gazebo_version.subprocess.run") as run:
            run.return_value.returncode = 0
            run.return_value.stdout = "8.14.0\n"

            result = gazebo_version(Path("/opt/gz"))

        self.assertEqual(result, "8.14.0")
        self.assertEqual(run.call_args.args[0], ["/opt/gz", "sim", "--versions"])

    def test_returns_none_when_version_command_fails(self) -> None:
        with patch("gazebo_version.subprocess.run") as run:
            run.return_value.returncode = 255
            run.return_value.stdout = ""

            self.assertIsNone(gazebo_version("/opt/gz"))

    def test_reports_installed_gazebo_version(self) -> None:
        binary = Path("/Users/vincent/Work/gz-macOS/ws/install/bin/gz")
        if not binary.is_file():
            self.skipTest("source-built Gazebo is unavailable")

        try:
            result = subprocess.run(
                [str(binary), "sim", "--versions"],
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )
        except subprocess.TimeoutExpired:
            self.skipTest("source-built Gazebo CLI exceeded the integration-test time budget")

        if result.returncode in {-9, 137}:
            self.skipTest("source-built Gazebo CLI was killed by host resource pressure")
        self.assertEqual(result.returncode, 0, result.stderr)
        version = next((line.strip() for line in result.stdout.splitlines() if line.strip()), None)

        self.assertIsNotNone(version)
        self.assertRegex(version or "", r"^\d+\.\d+\.\d+$")


if __name__ == "__main__":
    unittest.main()
