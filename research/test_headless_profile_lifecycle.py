from __future__ import annotations

import signal
import subprocess
import unittest
from unittest import mock

import run_headless_profile_lifecycle as lifecycle


class WrapperCleanupTests(unittest.TestCase):
    def make_process(self, wait_results: list[object], *, returncode: int | None = None):
        process = mock.Mock()
        process.pid = 4321
        process.poll.return_value = returncode
        process.wait.side_effect = wait_results
        return process

    def test_escalates_only_the_run_owned_group_after_sigint_timeout(self) -> None:
        process = self.make_process(
            [
                subprocess.TimeoutExpired("wrapper", lifecycle.STOP_TIMEOUT_SECONDS),
                0,
            ]
        )
        report = {}

        with mock.patch.object(lifecycle.os, "killpg") as killpg:
            lifecycle._stop_wrapper_process(process, report)

        process.send_signal.assert_called_once_with(signal.SIGINT)
        killpg.assert_called_once_with(process.pid, signal.SIGTERM)
        self.assertEqual(report["cleanupEscalation"], ["SIGTERM"])
        self.assertNotIn("cleanupStopFailure", report)

    def test_escalates_to_sigkill_and_records_a_stuck_process_group(self) -> None:
        process = self.make_process(
            [
                subprocess.TimeoutExpired("wrapper", lifecycle.STOP_TIMEOUT_SECONDS),
                subprocess.TimeoutExpired("wrapper", lifecycle.FORCED_STOP_TIMEOUT_SECONDS),
                subprocess.TimeoutExpired("wrapper", lifecycle.FORCED_STOP_TIMEOUT_SECONDS),
            ]
        )
        report = {}

        with mock.patch.object(lifecycle.os, "killpg") as killpg:
            lifecycle._stop_wrapper_process(process, report)

        self.assertEqual(
            killpg.call_args_list,
            [
                mock.call(process.pid, signal.SIGTERM),
                mock.call(process.pid, signal.SIGKILL),
            ],
        )
        self.assertEqual(report["cleanupEscalation"], ["SIGTERM", "SIGKILL"])
        self.assertIn("remained alive", report["cleanupStopFailure"])

    def test_does_not_signal_an_already_exited_wrapper(self) -> None:
        process = self.make_process([], returncode=0)
        report = {}

        with mock.patch.object(lifecycle.os, "killpg") as killpg:
            lifecycle._stop_wrapper_process(process, report)

        process.send_signal.assert_not_called()
        process.wait.assert_not_called()
        killpg.assert_not_called()
        self.assertEqual(report, {})


if __name__ == "__main__":
    unittest.main()
