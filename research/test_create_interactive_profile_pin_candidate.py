from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from create_interactive_profile_pin_candidate import (
    create_candidate,
    read_run_input,
    validate_run_evidence,
)
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
                    "worktreeStatusSha256": "a" * 64,
                    "trackedDiffSha256": "b" * 64,
                    "dirtyFilesSha256": "c" * 64,
                    "dirtyFileCount": 0,
                }
            },
            "artifacts": {"qgroundcontrolBinarySha256": "d" * 64},
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

    def test_run_input_must_be_a_regular_file_inside_the_run(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            run_dir = root / "run-123"
            run_dir.mkdir()
            receipt_path = run_dir / "receipt.json"
            receipt_path.write_text('{"runId":"run-123"}', encoding="utf-8")
            self.assertEqual(read_run_input(run_dir, receipt_path, "receipt"), {"runId": "run-123"})

            outside = root / "outside.json"
            outside.write_text('{"runId":"run-123"}', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "inside the receipt run directory"):
                read_run_input(run_dir, outside, "comparison")

            alias = root / "linked-run"
            alias.symlink_to(run_dir, target_is_directory=True)
            with self.assertRaisesRegex(ValueError, "run directory is a symlink"):
                read_run_input(alias, alias / "receipt.json", "receipt")

    def test_run_input_rejects_duplicate_identity_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = Path(temporary) / "run-123"
            run_dir.mkdir()
            receipt_path = run_dir / "receipt.json"
            receipt_path.write_text('{"runId":"other-run","runId":"run-123"}')
            with self.assertRaisesRegex(ValueError, "invalid JSON"):
                read_run_input(run_dir, receipt_path, "receipt")

    def test_run_input_rejects_symlinked_receipt_or_comparison(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            run_dir = root / "run-123"
            run_dir.mkdir()
            external = root / "external.json"
            external.write_text('{"runId":"run-123"}', encoding="utf-8")
            for name in ("receipt.json", "comparison.json"):
                with self.subTest(name=name):
                    linked = run_dir / name
                    linked.symlink_to(external)
                    with self.assertRaisesRegex(ValueError, "symlink"):
                        read_run_input(run_dir, linked, name)
                    linked.unlink()

    def test_candidate_rejects_different_run_ids(self) -> None:
        self.comparison["runId"] = "other-run"
        with self.assertRaisesRegex(ValueError, "run IDs differ"):
            create_candidate(self.receipt, self.comparison)

    def test_candidate_rejects_missing_source_digests(self) -> None:
        del self.receipt["source"]["antoine"]["trackedDiffSha256"]
        with self.assertRaisesRegex(ValueError, "source digests are missing or malformed"):
            create_candidate(self.receipt, self.comparison)

    def test_candidate_rejects_malformed_source_digest(self) -> None:
        self.receipt["source"]["antoine"]["trackedDiffSha256"] = "not-a-digest"
        with self.assertRaisesRegex(ValueError, "source digests are missing or malformed"):
            create_candidate(self.receipt, self.comparison)

    def test_candidate_rejects_boolean_dirty_file_count(self) -> None:
        self.receipt["source"]["antoine"]["dirtyFileCount"] = True
        with self.assertRaisesRegex(ValueError, "dirty-file count"):
            create_candidate(self.receipt, self.comparison)

    def test_candidate_rejects_malformed_artifact_digest(self) -> None:
        self.receipt["artifacts"]["qgroundcontrolBinarySha256"] = "not-a-digest"
        with self.assertRaisesRegex(ValueError, "malformed SHA-256"):
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
            "bridgeBinarySha256": "e" * 64,
        }
        candidate = create_candidate(self.receipt, self.comparison)
        self.assertEqual(candidate["artifacts"], {"bridgeBinarySha256": "e" * 64})
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
