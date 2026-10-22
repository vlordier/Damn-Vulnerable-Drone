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
                " 10 1 2.0 0.5 00:10 nats-server\n"
                " 11 1 40.0 1.0 00:05 gz\n"
                " 12 1 10.0 0.2 00:02 node\n"
                " 13 1 30.0 0.4 00:01 rustc\n"
                " 14 1 20.0 0.3 00:01 cmake\n"
                " 15 1 50.0 1.2 00:04 Google Chrome He\n"
                "invalid line\n"
            ),
            stderr="",
        )

        result = process_inventory()

        self.assertEqual(
            run.call_args.args[0], ["ps", "-Ao", "pid=,ppid=,%cpu=,%mem=,etime=,ucomm="]
        )
        self.assertEqual(result["status"], "captured")
        self.assertEqual(result["topCpuProcesses"][0]["pid"], 15)
        self.assertEqual(result["topCpuProcesses"][0]["name"], "Google Chrome He")
        self.assertEqual(
            [item["name"] for item in result["simulationAndBuildProcesses"]],
            ["gz", "rustc", "cmake", "node", "nats-server"],
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
