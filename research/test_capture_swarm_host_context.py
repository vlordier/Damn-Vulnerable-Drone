from __future__ import annotations

import subprocess
import unittest
from unittest import mock

from capture_swarm_host_context import process_inventory


class ProcessInventoryTests(unittest.TestCase):
    @mock.patch("capture_swarm_host_context.subprocess.run")
    def test_sorts_cpu_processes_and_keeps_simulation_names_without_command_args(
        self, run: mock.Mock
    ) -> None:
        run.return_value = subprocess.CompletedProcess(
            args=["ps"],
            returncode=0,
            stdout=(
                " 10 1 2.0 0.5 00:10 /usr/local/bin/nats-server\n"
                " 11 1 40.0 1.0 00:05 /Applications/Gazebo.app/Contents/MacOS/gz sim -g\n"
                " 12 1 10.0 0.2 00:02 /usr/bin/node\n"
                "invalid line\n"
            ),
            stderr="",
        )

        result = process_inventory()

        self.assertEqual(result["status"], "captured")
        self.assertEqual(result["topCpuProcesses"][0]["pid"], 11)
        self.assertEqual(
            [item["name"] for item in result["simulationAndBuildProcesses"]],
            ["gz", "nats-server"],
        )
        self.assertNotIn("command", result["topCpuProcesses"][0])

    @mock.patch("capture_swarm_host_context.subprocess.run")
    def test_reports_process_inventory_unavailable_on_ps_failure(self, run: mock.Mock) -> None:
        run.return_value = subprocess.CompletedProcess(
            args=["ps"], returncode=1, stdout="", stderr="permission denied"
        )
        self.assertEqual(process_inventory(), {"status": "unavailable", "reason": "ps-failed"})


if __name__ == "__main__":
    unittest.main()
