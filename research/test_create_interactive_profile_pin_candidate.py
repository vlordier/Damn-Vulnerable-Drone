from __future__ import annotations

import unittest

from create_interactive_profile_pin_candidate import create_candidate
from verify_interactive_profile_pins import verify


class InteractiveProfilePinCandidateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.receipt = {
            "contract": "damn-vulnerable-drone.interactive-profile",
            "version": "1.0.0",
            "runId": "run-123",
            "profile": "interactive-antoine-gazebo",
            "source": {
                "antoine": {
                    "revision": "a1",
                    "classification": "clean",
                    "worktreeStatusSha256": "status-a",
                    "trackedDiffSha256": "diff-a",
                    "dirtyFilesSha256": "dirty-a",
                    "dirtyFileCount": 0,
                }
            },
            "artifacts": {"qgroundcontrolBinarySha256": "qgc-a"},
            "toolchain": {"rustc": "rustc 1.99.1"},
        }
        self.comparison = {
            "contract": "damn-vulnerable-drone.antoine-provenance-comparison",
            "version": "1.0.0",
            "runId": "run-123",
            "stable": True,
            "prePhaseValid": True,
            "postPhaseValid": True,
            "profileMatches": True,
            "toolchainMatches": True,
            "patchMatches": True,
            "changedSourceTrees": [],
            "changedInputs": [],
        }

    def test_candidate_requires_stable_complete_matching_run(self) -> None:
        candidate = create_candidate(self.receipt, self.comparison)
        self.assertEqual(candidate["review"]["status"], "unreviewed")
        self.assertEqual(candidate["source"], self.receipt["source"])
        self.assertEqual(verify(self.receipt, candidate)["status"], "missing")

        candidate["review"] = {
            "status": "reviewed",
            "reviewer": "operator",
            "reviewedAtUtc": "2026-10-21T12:00:00Z",
        }
        self.assertEqual(verify(self.receipt, candidate)["status"], "verified")

    def test_candidate_rejects_unstable_comparison(self) -> None:
        self.comparison["stable"] = False
        with self.assertRaisesRegex(ValueError, "stable provenance comparison"):
            create_candidate(self.receipt, self.comparison)

    def test_candidate_rejects_different_run_ids(self) -> None:
        self.comparison["runId"] = "other-run"
        with self.assertRaisesRegex(ValueError, "run IDs differ"):
            create_candidate(self.receipt, self.comparison)

    def test_candidate_rejects_missing_source_digests(self) -> None:
        del self.receipt["source"]["antoine"]["trackedDiffSha256"]
        with self.assertRaisesRegex(ValueError, "source digests are incomplete"):
            create_candidate(self.receipt, self.comparison)


if __name__ == "__main__":
    unittest.main()
