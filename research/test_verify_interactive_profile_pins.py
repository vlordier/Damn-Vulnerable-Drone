from __future__ import annotations

import unittest

from verify_interactive_profile_pins import PIN_CONTRACT, verify


class InteractiveProfilePinTests(unittest.TestCase):
    def setUp(self) -> None:
        self.receipt = {
            "contract": "damn-vulnerable-drone.interactive-profile",
            "version": "1.0.0",
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
        self.pins = {
            "contract": PIN_CONTRACT,
            "version": "1.0.0",
            "profile": "interactive-antoine-gazebo",
            "source": {"antoine": dict(self.receipt["source"]["antoine"])},
            "artifacts": dict(self.receipt["artifacts"]),
            "toolchain": dict(self.receipt["toolchain"]),
        }

    def test_exact_reviewed_inputs_are_verified(self) -> None:
        result = verify(self.receipt, self.pins)
        self.assertEqual(result["status"], "verified")
        self.assertTrue(result["verified"])

    def test_changed_source_revision_is_incompatible(self) -> None:
        self.receipt["source"]["antoine"]["revision"] = "a2"
        result = verify(self.receipt, self.pins)
        self.assertEqual(result["status"], "incompatible")
        self.assertIn("source.antoine.revision differs from reviewed pin", result["incompatible"])

    def test_changed_artifact_digest_is_incompatible(self) -> None:
        self.receipt["artifacts"]["qgroundcontrolBinarySha256"] = "qgc-b"
        result = verify(self.receipt, self.pins)
        self.assertEqual(result["status"], "incompatible")

    def test_missing_artifact_digest_is_missing(self) -> None:
        self.receipt["artifacts"]["qgroundcontrolBinarySha256"] = None
        result = verify(self.receipt, self.pins)
        self.assertEqual(result["status"], "missing")

    def test_dirty_source_without_all_reviewed_digests_is_unreviewed(self) -> None:
        source = self.receipt["source"]["antoine"]
        source["classification"] = "dirty-unreviewed"
        self.pins["source"]["antoine"]["classification"] = "dirty-unreviewed"
        self.pins["source"]["antoine"].pop("dirtyFilesSha256")
        result = verify(self.receipt, self.pins)
        self.assertEqual(result["status"], "dirty-unreviewed")
        self.assertTrue(result["dirtyUnreviewed"])

    def test_unavailable_receipt_section_is_missing(self) -> None:
        del self.receipt["toolchain"]
        result = verify(self.receipt, self.pins)
        self.assertEqual(result["status"], "missing")

    def test_wrong_contract_fails_closed(self) -> None:
        self.receipt["contract"] = "other"
        result = verify(self.receipt, self.pins)
        self.assertEqual(result["status"], "missing")

    def test_unsupported_receipt_version_fails_closed(self) -> None:
        self.receipt["version"] = "2.0.0"
        result = verify(self.receipt, self.pins)
        self.assertEqual(result["status"], "missing")

    def test_unavailable_source_classification_fails_closed(self) -> None:
        self.receipt["source"]["antoine"]["classification"] = "unavailable"
        result = verify(self.receipt, self.pins)
        self.assertEqual(result["status"], "missing")


if __name__ == "__main__":
    unittest.main()
