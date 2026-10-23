"""Adversarial eligibility checks for the Antoine baseline evidence summary."""

from __future__ import annotations

import unittest
from pathlib import Path

from summarize_antoine_baseline import build_baseline_result


class AntoineBaselineEligibilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.run_dir = Path("baseline-run")
        self.proof = {
            "localGrant": {"runId": self.run_dir.name},
            "localFlightPassed": True,
            "poseTimelinePassed": True,
            "actuatorNeutralizationPassed": True,
            "physicalSafeStopPassed": None,
            "measured": {
                "allLegsReached": True,
                "settledAtGround": True,
                "groundSettleDwellSecondsObserved": 1.0,
            },
        }

    def test_measured_ground_dwell_qualifies_flight_control(self) -> None:
        result = build_baseline_result(self.run_dir, self.proof, 0)

        self.assertTrue(result["baselineEligible"])
        self.assertTrue(result["gates"]["groundSettlePassed"])
        self.assertIs(result["measurements"]["settledAtGround"], True)
        self.assertEqual(result["measurements"]["groundSettleDwellSecondsObserved"], 1.0)

    def test_missing_ground_dwell_fails_closed(self) -> None:
        for state in ("missing", None, False):
            with self.subTest(state=state):
                proof = {**self.proof, "measured": dict(self.proof["measured"])}
                if state == "missing":
                    proof["measured"].pop("settledAtGround")
                else:
                    proof["measured"]["settledAtGround"] = state

                result = build_baseline_result(self.run_dir, proof, 0)

                self.assertFalse(result["gates"]["groundSettlePassed"])
                self.assertFalse(result["baselineEligible"])

    def test_stop_claim_without_truth_touchdown_fails(self) -> None:
        proof = {
            **self.proof,
            "measured": {**self.proof["measured"], "settledAtGround": None},
        }

        result = build_baseline_result(self.run_dir, proof, 0)

        self.assertFalse(result["gates"]["groundSettlePassed"])
        self.assertFalse(result["baselineEligible"])

    def test_ground_dwell_rejects_boolean_non_finite_and_unmeasured_values(self) -> None:
        for dwell in (True, float("inf"), float("nan"), "1.0", 0.999):
            with self.subTest(dwell=dwell):
                proof = {**self.proof, "measured": dict(self.proof["measured"])}
                proof["measured"]["groundSettleDwellSecondsObserved"] = dwell

                result = build_baseline_result(self.run_dir, proof, 0)

                self.assertFalse(result["gates"]["groundSettlePassed"])
                self.assertFalse(result["baselineEligible"])

    def test_nonzero_launcher_exit_fails_even_with_measured_stop(self) -> None:
        result = build_baseline_result(self.run_dir, self.proof, 1)

        self.assertFalse(result["gates"]["launcherExitZero"])
        self.assertFalse(result["baselineEligible"])


if __name__ == "__main__":
    unittest.main()
