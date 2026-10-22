from __future__ import annotations

import unittest

from swarm_controller_diagnostics import state_coalescing_diagnostic


class StateCoalescingDiagnosticTests(unittest.TestCase):
    def test_enabled_without_backlog_measures_zero(self) -> None:
        result = state_coalescing_diagnostic(
            ["ANTOINE_GZ_STATE_COALESCING_ENABLED callsign=drone-1"], "drone-1"
        )
        self.assertEqual(result["status"], "measured")
        self.assertEqual(result["droppedSupersededMessages"], 0)

    def test_backlog_events_are_summed_only_for_selected_vehicle(self) -> None:
        result = state_coalescing_diagnostic(
            [
                "ANTOINE_GZ_STATE_COALESCING_ENABLED callsign=drone-1",
                "ANTOINE_GZ_STATE_COALESCED callsign=drone-1 dropped_superseded=2",
                "ANTOINE_GZ_STATE_COALESCED callsign=drone-1 dropped_superseded=5",
                "ANTOINE_GZ_STATE_COALESCED callsign=drone-2 dropped_superseded=99",
            ],
            "drone-1",
        )
        self.assertEqual(result["status"], "measured")
        self.assertEqual(result["eventCount"], 2)
        self.assertEqual(result["droppedSupersededMessages"], 7)

    def test_missing_enable_marker_is_unavailable_not_zero(self) -> None:
        result = state_coalescing_diagnostic([], "drone-1")
        self.assertEqual(result["status"], "unavailable")
        self.assertIsNone(result["droppedSupersededMessages"])

    def test_malformed_marker_or_events_without_enable_marker_are_invalid(self) -> None:
        malformed = state_coalescing_diagnostic(
            [
                "ANTOINE_GZ_STATE_COALESCING_ENABLED callsign=drone-1",
                "ANTOINE_GZ_STATE_COALESCED callsign=drone-1 dropped_superseded=zero",
            ],
            "drone-1",
        )
        unenabled_event = state_coalescing_diagnostic(
            ["ANTOINE_GZ_STATE_COALESCED callsign=drone-1 dropped_superseded=1"],
            "drone-1",
        )
        self.assertEqual(malformed["status"], "invalid")
        self.assertEqual(unenabled_event["status"], "invalid")


if __name__ == "__main__":
    unittest.main()
