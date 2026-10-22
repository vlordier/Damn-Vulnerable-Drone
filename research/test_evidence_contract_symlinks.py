from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from build_evidence_manifest import build, read_json, write_manifest
from dvd_research.evidence_contract import verify_manifest, verify_signature


class EvidenceManifestSymlinkTests(unittest.TestCase):
    def test_manifest_reader_rejects_duplicate_qualification_fields(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory) / "run-a"
            run_dir.mkdir()
            manifest_path = run_dir / "research-evidence-manifest.json"
            manifest_path.write_text(
                '{"contract":"damn-vulnerable-drone.antoine-evidence-manifest",'
                '"runId":"wrong-run","runId":"run-a","publicEvidence":[]}',
                encoding="utf-8",
            )
            self.assertEqual(verify_manifest(manifest_path)[0], False)

    def test_manifest_builder_rejects_duplicate_summary_status(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "research-result.json"
            path.write_text('{"status":"failed","status":"passed"}', encoding="utf-8")
            self.assertIsNone(read_json(path))

    def test_signature_verifier_rejects_duplicate_sidecar_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory) / "run-a"
            run_dir.mkdir()
            manifest_path = run_dir / "research-evidence-manifest.json"
            manifest_path.write_text("{}", encoding="utf-8")
            (run_dir / "research-evidence-signature.json").write_text(
                '{"contract":"wrong","contract":"damn-vulnerable-drone.antoine-evidence-signature"}',
                encoding="utf-8",
            )

            status, _errors = verify_signature(manifest_path)

        self.assertEqual(status, "invalid_signature")

    def test_duplicate_applier_session_event_does_not_qualify_audit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory) / "run-a"
            audit_dir = run_dir / "applier-neutral"
            audit_dir.mkdir(parents=True)
            (audit_dir / "applier-session.jsonl").write_text(
                '{"event":"invalid","event":"session-start"}\n{"event":"clean-shutdown"}\n',
                encoding="utf-8",
            )
            manifest = build(run_dir)
        self.assertFalse(manifest["measurements"]["applierSessionAuditComplete"])

    def test_builder_exports_and_marks_incomplete_applier_session_audit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory) / "run-a"
            audit_dir = run_dir / "applier-neutral"
            audit_dir.mkdir(parents=True)
            audit = audit_dir / "applier-session.jsonl"
            audit.write_text(
                '{"event":"session-start"}\n{"event":"publisher-inventory"}\n',
                encoding="utf-8",
            )

            manifest = build(run_dir)

        names = {item["name"] for item in manifest["publicEvidence"]}
        self.assertIn("applier-neutral/applier-session.jsonl", names)
        self.assertIs(manifest["measurements"]["applierSessionAuditComplete"], False)
        self.assertEqual(manifest["measurements"]["applierSessionAuditEventCount"], 2)

    def test_builder_marks_clean_applier_session_audit_complete(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory) / "run-a"
            audit_dir = run_dir / "applier-neutral"
            audit_dir.mkdir(parents=True)
            (audit_dir / "applier-session.jsonl").write_text(
                '{"event":"session-start"}\n{"event":"clean-shutdown"}\n',
                encoding="utf-8",
            )

            manifest = build(run_dir)

        self.assertIs(manifest["measurements"]["applierSessionAuditComplete"], True)

    def test_symlinked_applier_audit_is_incomplete_and_never_exported(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run_dir = root / "run-a"
            run_dir.mkdir()
            private_audit = root / "private-session.jsonl"
            private_audit.write_text(
                '{"event":"session-start"}\n{"event":"clean-shutdown"}\n',
                encoding="utf-8",
            )
            audit_dir = run_dir / "applier-neutral"
            audit_dir.mkdir()
            (audit_dir / "applier-session.jsonl").symlink_to(private_audit)

            manifest = build(run_dir)

        self.assertIs(manifest["measurements"]["applierSessionAuditComplete"], False)
        self.assertNotIn(
            "applier-neutral/applier-session.jsonl",
            {item["name"] for item in manifest["publicEvidence"]},
        )

    def test_applier_audit_under_symlinked_parent_is_incomplete(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run_dir = root / "run-a"
            run_dir.mkdir()
            outside = root / "outside"
            outside.mkdir()
            (outside / "applier-session.jsonl").write_text(
                '{"event":"session-start"}\n{"event":"clean-shutdown"}\n',
                encoding="utf-8",
            )
            (run_dir / "applier-neutral").symlink_to(outside, target_is_directory=True)

            manifest = build(run_dir)

        self.assertIs(manifest["measurements"]["applierSessionAuditComplete"], False)

    def test_incomplete_applier_audit_downgrades_otherwise_qualified_swarm_run(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory) / "run-a"
            run_dir.mkdir()
            (run_dir / "research-swarm-flight.json").write_text(
                json.dumps(
                    {
                        "runId": run_dir.name,
                        "status": "passed",
                        "allVehicleRunIdentityMatches": True,
                        "allFlightsPassed": True,
                        "baselineStatus": "qualified_baseline",
                        "baselineGates": {"gazeboAttitudeWithinEnvelope": True},
                        "poseStreamQualityPassed": True,
                        "allPairwiseSeparationsMeasured": True,
                        "allPairwiseSeparationsAtLeast1m": True,
                        "behaviorPassed": True,
                    }
                ),
                encoding="utf-8",
            )
            (run_dir / "research-provenance-comparison.json").write_text(
                json.dumps(
                    {
                        "runId": run_dir.name,
                        "prePhaseValid": True,
                        "postPhaseValid": True,
                        "profileMatches": True,
                        "stable": True,
                    }
                ),
                encoding="utf-8",
            )
            audit_dir = run_dir / "applier-neutral"
            audit_dir.mkdir()
            (audit_dir / "applier-session.jsonl").write_text(
                '{"event":"session-start"}\n{"event":"publisher-inventory"}\n',
                encoding="utf-8",
            )

            manifest = build(run_dir)

        self.assertEqual(manifest["measurementClassification"], "inconclusive")
        self.assertIn("applier session audit is incomplete", manifest["qualification"])

    def test_builder_includes_two_drone_smoke_and_gazebo_truth_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory) / "run-a"
            run_dir.mkdir()
            (run_dir / "research-two-drone-smoke.json").write_text('{"status":"passed"}\n')
            (run_dir / "research-run-failure.txt").write_text(
                "runId=run-a\nstage=start-gazebo\nexitCode=1\n", encoding="utf-8"
            )
            (run_dir / "gazebo-pose-info.jsonl").write_text('{"pose":[]}\n')

            manifest = build(run_dir)

        names = {item["name"] for item in manifest["publicEvidence"]}
        self.assertIn("research-two-drone-smoke.json", names)
        self.assertIn("research-run-failure.txt", names)
        self.assertIn("gazebo-pose-info.jsonl", names)

    def test_builder_omits_allowlisted_file_under_symlinked_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory) / "run-a"
            run_dir.mkdir()
            private_dir = run_dir / "private"
            private_dir.mkdir()
            (private_dir / "actuator-output.jsonl").write_text("private audit\n")
            (run_dir / "applier-neutral").symlink_to(private_dir, target_is_directory=True)

            manifest = build(run_dir)

        names = {item["name"] for item in manifest["publicEvidence"]}
        self.assertNotIn("applier-neutral/actuator-output.jsonl", names)

    def test_symlinked_swarm_report_cannot_drive_qualification(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory) / "run-a"
            run_dir.mkdir()
            private_dir = run_dir / "private"
            private_dir.mkdir()
            report = {
                "runId": run_dir.name,
                "scenario": "independent-antoine-controllers-gazebo-nats",
                "status": "passed",
                "allVehicleRunIdentityMatches": True,
                "allFlightsPassed": True,
                "baselineStatus": "qualified_baseline",
                "baselineGates": {"gazeboAttitudeWithinEnvelope": True},
                "poseStreamQualityPassed": True,
                "allPairwiseSeparationsMeasured": True,
                "allPairwiseSeparationsAtLeast1m": True,
                "behaviorPassed": True,
            }
            (private_dir / "swarm-flight.json").write_text(json.dumps(report))
            (run_dir / "research-swarm-flight.json").symlink_to(private_dir / "swarm-flight.json")
            (run_dir / "research-provenance-comparison.json").write_text(
                json.dumps(
                    {
                        "runId": run_dir.name,
                        "prePhaseValid": True,
                        "postPhaseValid": True,
                        "profileMatches": True,
                        "stable": True,
                    }
                )
            )

            manifest = build(run_dir)

        self.assertEqual(manifest["measurementClassification"], "inconclusive")
        self.assertIsNot(manifest["measurements"].get("swarmFlightPassed"), True)
        self.assertNotIn(
            "research-swarm-flight.json", {item["name"] for item in manifest["publicEvidence"]}
        )

    def test_builder_rejects_symlinked_run_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory) / "run-a"
            run_dir.mkdir()
            alias = Path(directory) / "run-alias"
            alias.symlink_to(run_dir, target_is_directory=True)

            with self.assertRaisesRegex(ValueError, "symlinked run directory"):
                build(alias)

    def test_writer_refuses_manifest_symlink_without_changing_its_target(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory) / "run-a"
            run_dir.mkdir()
            outside = Path(directory) / "outside.json"
            outside.write_text("preserve this file\n")
            (run_dir / "research-evidence-manifest.json").symlink_to(outside)

            with self.assertRaisesRegex(ValueError, "symlinked evidence manifest"):
                write_manifest(run_dir, {"contract": "test"})

            self.assertEqual(outside.read_text(), "preserve this file\n")

    def test_writer_atomically_writes_a_verifiable_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory) / "run-a"
            run_dir.mkdir()
            evidence = run_dir / "research-result.json"
            content = b"{}\n"
            evidence.write_bytes(content)
            manifest = {
                "contract": "damn-vulnerable-drone.antoine-evidence-manifest",
                "runId": run_dir.name,
                "publicEvidence": [
                    {
                        "name": evidence.name,
                        "sizeBytes": len(content),
                        "sha256": hashlib.sha256(content).hexdigest(),
                    }
                ],
            }

            output = write_manifest(run_dir, manifest)

            self.assertEqual(output, run_dir.resolve() / "research-evidence-manifest.json")
            self.assertEqual(verify_manifest(output), (True, []))

    def test_verifier_rejects_allowlisted_file_under_symlinked_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory) / "run-a"
            run_dir.mkdir()
            private_dir = run_dir / "private"
            private_dir.mkdir()
            content = b"private audit\n"
            (private_dir / "actuator-output.jsonl").write_bytes(content)
            (run_dir / "applier-neutral").symlink_to(private_dir, target_is_directory=True)
            manifest_path = run_dir / "research-evidence-manifest.json"
            manifest_path.write_text(
                json.dumps(
                    {
                        "contract": "damn-vulnerable-drone.antoine-evidence-manifest",
                        "runId": run_dir.name,
                        "publicEvidence": [
                            {
                                "name": "applier-neutral/actuator-output.jsonl",
                                "sizeBytes": len(content),
                                "sha256": hashlib.sha256(content).hexdigest(),
                            }
                        ],
                    }
                )
            )

            valid, errors = verify_manifest(manifest_path)

        self.assertFalse(valid)
        self.assertTrue(any("evidence file is missing or escapes" in error for error in errors))


if __name__ == "__main__":
    unittest.main()
