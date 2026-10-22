from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from create_interactive_profile_pin_candidate import create_candidate, validate_run_evidence
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

    def test_candidate_allows_unavailable_checkout_when_snapshot_is_pinned(self) -> None:
        self.receipt["source"]["gazeboAssets"] = {
            "name": "gazeboAssets",
            "classification": "unavailable",
            "revision": None,
            "reason": "source path is missing or is not a Git checkout",
        }
        self.receipt["sourceSnapshots"] = {
            "antoine": {
                "path": "antoine-source-snapshot",
                "receiptSha256": "a" * 64,
                "snapshotManifestSha256": "b" * 64,
                "fileCount": 142,
            },
            "gazebo": {
                "path": "gazebo-source-snapshot",
                "receiptSha256": "c" * 64,
                "snapshotManifestSha256": "d" * 64,
                "fileCount": 279,
            },
        }

        candidate = create_candidate(self.receipt, self.comparison)
        self.assertEqual(candidate["sourceSnapshots"], self.receipt["sourceSnapshots"])
        self.assertEqual(candidate["review"]["status"], "unreviewed")
        self.assertEqual(verify(self.receipt, candidate)["status"], "missing")

        candidate["review"] = {
            "status": "reviewed",
            "reviewer": "operator",
            "reviewedAtUtc": "2026-10-21T12:00:00Z",
        }
        self.assertEqual(verify(self.receipt, candidate)["status"], "verified")

    def test_candidate_rejects_malformed_snapshot_record(self) -> None:
        self.receipt["source"]["gazeboAssets"] = {
            "name": "gazeboAssets",
            "classification": "unavailable",
            "revision": None,
            "reason": "source path is missing or is not a Git checkout",
        }
        self.receipt["sourceSnapshots"] = {
            "antoine": {
                "path": "../antoine-source-snapshot",
                "receiptSha256": "a" * 64,
                "snapshotManifestSha256": "b" * 64,
                "fileCount": 142,
            },
            "gazebo": {
                "path": "gazebo-source-snapshot",
                "receiptSha256": "c" * 64,
                "snapshotManifestSha256": "d" * 64,
                "fileCount": 279,
            },
        }
        with self.assertRaisesRegex(ValueError, "canonical relative POSIX path"):
            create_candidate(self.receipt, self.comparison)

    def test_candidate_rejects_unavailable_checkout_without_snapshot(self) -> None:
        self.receipt["source"]["gazeboAssets"] = {
            "name": "gazeboAssets",
            "classification": "unavailable",
            "revision": None,
        }
        with self.assertRaisesRegex(ValueError, "source state is unavailable"):
            create_candidate(self.receipt, self.comparison)

    def test_headless_candidate_omits_only_absent_qgc_artifacts(self) -> None:
        self.receipt["profile"] = "headless-antoine-gazebo-mavlink"
        self.receipt["artifacts"] = {
            "qgroundcontrolBinarySha256": None,
            "qgcSettingsSha256": None,
            "bridgeBinarySha256": "bridge-a",
        }
        candidate = create_candidate(self.receipt, self.comparison)
        self.assertEqual(candidate["artifacts"], {"bridgeBinarySha256": "bridge-a"})
        candidate["review"] = {
            "status": "reviewed",
            "reviewer": "operator",
            "reviewedAtUtc": "2026-10-21T12:00:00Z",
        }
        self.assertEqual(verify(self.receipt, candidate)["status"], "verified")

    def test_headless_candidate_requires_passing_disarmed_acceptance(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run_directory = Path(temporary)
            receipt = {
                "runId": run_directory.name,
                "runDirectory": str(run_directory),
                "profile": "headless-antoine-gazebo-mavlink",
                "callsign": "drone-1",
            }
            report_path = run_directory / "gazebo-mavlink-acceptance.json"
            report_path.write_text(
                json.dumps(
                    {
                        "schema": "dvd.gazebo-mavlink-flight-check.v1",
                        "passed": True,
                        "validGazeboTelemetryReceived": True,
                        "callsign": "drone-1",
                        "finalMavlinkState": {"armed": False},
                        "finalGazeboPose": [0.0, 0.0, 0.15],
                    }
                ),
                encoding="utf-8",
            )
            validate_run_evidence(receipt, run_directory)

            report_path.write_text(
                json.dumps(
                    {
                        "schema": "dvd.gazebo-mavlink-flight-check.v1",
                        "passed": False,
                        "validGazeboTelemetryReceived": True,
                        "callsign": "drone-1",
                        "finalMavlinkState": {"armed": False},
                        "finalGazeboPose": [0.0, 0.0, 0.15],
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "passing disarmed Gazebo acceptance"):
                validate_run_evidence(receipt, run_directory)

    def test_headless_candidate_requires_valid_gazebo_telemetry_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run_directory = Path(temporary)
            receipt = {
                "runId": run_directory.name,
                "runDirectory": str(run_directory),
                "profile": "headless-antoine-gazebo-mavlink",
                "callsign": "drone-1",
            }
            report_path = run_directory / "gazebo-mavlink-acceptance.json"
            report_path.write_text(
                json.dumps(
                    {
                        "schema": "dvd.gazebo-mavlink-flight-check.v1",
                        "passed": True,
                        "callsign": "drone-1",
                        "finalMavlinkState": {"armed": False},
                        "finalGazeboPose": [0.0, 0.0, 0.15],
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "passing disarmed Gazebo acceptance"):
                validate_run_evidence(receipt, run_directory)


if __name__ == "__main__":
    unittest.main()
