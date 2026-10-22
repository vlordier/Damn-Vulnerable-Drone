from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from wait_interactive_profile_ready import wait_until_recorded


class WaitForProfileReadinessTests(unittest.TestCase):
    def test_waits_for_recorded_disarmed_status(self) -> None:
        current_time = [0.0]
        results = iter(
            [
                {"status": "partial", "armed": None},
                {"status": "recorded", "armed": True},
                {"status": "recorded", "armed": False, "bridgeHeartbeat": {"status": "observed"}},
            ]
        )
        calls = []

        def inspect(receipt: Path) -> dict:
            calls.append(receipt)
            return next(results)

        with tempfile.TemporaryDirectory() as directory:
            receipt = Path(directory) / "interactive-profile-receipt.json"
            result = wait_until_recorded(
                receipt,
                timeout_seconds=3,
                poll_interval_seconds=0.5,
                inspect=inspect,
                monotonic=lambda: current_time[0],
                sleep=lambda duration: current_time.__setitem__(0, current_time[0] + duration),
            )

        self.assertEqual(result["status"], "recorded")
        self.assertIs(result["armed"], False)
        self.assertEqual(len(calls), 3)

    def test_timeout_includes_the_last_observed_readiness_gap(self) -> None:
        current_time = [0.0]
        with self.assertRaisesRegex(TimeoutError, "last status.*bridgeHeartbeat"):
            wait_until_recorded(
                Path("receipt.json"),
                timeout_seconds=1,
                poll_interval_seconds=0.4,
                inspect=lambda _receipt: {
                    "status": "partial",
                    "armed": None,
                    "bridgeHeartbeat": {"status": "stale"},
                },
                monotonic=lambda: current_time[0],
                sleep=lambda duration: current_time.__setitem__(0, current_time[0] + duration),
            )

    def test_timeout_and_poll_intervals_are_bounded(self) -> None:
        for timeout, interval in ((0, 1), (301, 1), (1, 0), (1, 11)):
            with self.subTest(timeout=timeout, interval=interval):
                with self.assertRaises(ValueError):
                    wait_until_recorded(Path("receipt.json"), timeout, interval)


if __name__ == "__main__":
    unittest.main()
