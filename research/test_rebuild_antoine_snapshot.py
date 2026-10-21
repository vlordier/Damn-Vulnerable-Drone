"""Receipt and source-bundle checks for Antoine clean rebuilds."""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import rebuild_antoine_snapshot as rebuild

TOOLCHAIN = {
    "cargo": "cargo 1.99.1 (receipt)",
    "rustc": "rustc 1.99.1 (receipt)",
    "gazebo": "8.14.0",
}


def write_profile_run(run_dir: Path) -> None:
    snapshot = run_dir / "antoine-source-snapshot"
    (snapshot / "antoine/sitl").mkdir(parents=True)
    (snapshot / "ew-wire-contract/src").mkdir(parents=True)
    source = snapshot / "antoine/sitl/Cargo.toml"
    source.write_text("[package]\nname='fixture'\n")
    files = [
        {
            "path": source.relative_to(snapshot).as_posix(),
            "sizeBytes": source.stat().st_size,
            "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        }
    ]
    manifest = hashlib.sha256()
    for item in files:
        manifest.update(item["path"].encode())
        manifest.update(b"\0")
        manifest.update(item["sha256"].encode("ascii"))
        manifest.update(b"\n")
    source_snapshot = {
        "contract": "damn-vulnerable-drone.antoine-source-snapshot",
        "version": "1.1.0",
        "status": "verified",
        "snapshotManifestSha256": manifest.hexdigest(),
        "files": files,
    }
    snapshot_receipt = snapshot / "antoine-source-snapshot.json"
    snapshot_receipt.write_text(json.dumps(source_snapshot) + "\n")
    receipt_hash = hashlib.sha256(snapshot_receipt.read_bytes()).hexdigest()
    (run_dir / "interactive-profile-receipt.json").write_text(
        json.dumps(
            {
                "contract": "damn-vulnerable-drone.interactive-profile",
                "runId": run_dir.name,
                "status": "ready",
                "toolchain": TOOLCHAIN,
                "sourceSnapshots": {
                    "antoine": {
                        "path": "antoine-source-snapshot",
                        "receiptSha256": receipt_hash,
                        "snapshotManifestSha256": manifest.hexdigest(),
                        "fileCount": len(files),
                    }
                },
            }
        )
        + "\n"
    )
    (run_dir / "gazebo-mavlink-acceptance.json").write_text(
        json.dumps(
            {
                "schema": "dvd.gazebo-mavlink-flight-check.v1",
                "passed": True,
                "validGazeboTelemetryReceived": True,
                "prearmReadyBeforeFlight": True,
                "finalMavlinkState": {"armed": False},
            }
        )
        + "\n"
    )
    (run_dir / "research-provenance-comparison.json").write_text(
        json.dumps({"runId": run_dir.name, "stable": True}) + "\n"
    )


class AntoineSnapshotRebuildTests(unittest.TestCase):
    def test_profile_receipt_and_snapshot_bytes_are_verified(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory) / "profile-run"
            run_dir.mkdir()
            write_profile_run(run_dir)
            with (
                mock.patch.object(
                    rebuild, "command_output", side_effect=[TOOLCHAIN["cargo"], TOOLCHAIN["rustc"]]
                ),
                mock.patch.object(rebuild, "gazebo_version", return_value=TOOLCHAIN["gazebo"]),
            ):
                result = rebuild.verify_profile_inputs(run_dir)
            self.assertEqual(result["profile"]["runId"], "profile-run")
            self.assertEqual(result["snapshot"]["fileCount"], 1)

    def test_changed_snapshot_bytes_fail_before_rebuild(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory) / "profile-run"
            run_dir.mkdir()
            write_profile_run(run_dir)
            (run_dir / "antoine-source-snapshot/antoine/sitl/Cargo.toml").write_text("tampered\n")
            with (
                mock.patch.object(
                    rebuild, "command_output", side_effect=[TOOLCHAIN["cargo"], TOOLCHAIN["rustc"]]
                ),
                mock.patch.object(rebuild, "gazebo_version", return_value=TOOLCHAIN["gazebo"]),
            ):
                with self.assertRaisesRegex(ValueError, "source snapshot bytes do not match"):
                    rebuild.verify_profile_inputs(run_dir)

    def test_toolchain_mismatch_fails_before_rebuild(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory) / "profile-run"
            run_dir.mkdir()
            write_profile_run(run_dir)
            with (
                mock.patch.object(
                    rebuild,
                    "command_output",
                    side_effect=["cargo 1.98.0", TOOLCHAIN["rustc"]],
                ),
                mock.patch.object(rebuild, "gazebo_version", return_value=TOOLCHAIN["gazebo"]),
            ):
                with self.assertRaisesRegex(ValueError, "differ from the profile receipt"):
                    rebuild.verify_profile_inputs(run_dir)

    def test_failed_acceptance_cannot_be_used_as_rebuild_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory) / "profile-run"
            run_dir.mkdir()
            write_profile_run(run_dir)
            acceptance_path = run_dir / "gazebo-mavlink-acceptance.json"
            acceptance = json.loads(acceptance_path.read_text())
            acceptance["passed"] = False
            acceptance_path.write_text(json.dumps(acceptance) + "\n")
            with self.assertRaisesRegex(ValueError, "passing headless acceptance"):
                rebuild.verify_profile_inputs(run_dir)

    def test_acceptance_without_gazebo_nats_telemetry_cannot_be_rebuilt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory) / "profile-run"
            run_dir.mkdir()
            write_profile_run(run_dir)
            acceptance_path = run_dir / "gazebo-mavlink-acceptance.json"
            acceptance = json.loads(acceptance_path.read_text())
            del acceptance["validGazeboTelemetryReceived"]
            acceptance_path.write_text(json.dumps(acceptance) + "\n")
            with self.assertRaisesRegex(ValueError, "passing headless acceptance"):
                rebuild.verify_profile_inputs(run_dir)

    def test_unstable_provenance_cannot_be_used_as_rebuild_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory) / "profile-run"
            run_dir.mkdir()
            write_profile_run(run_dir)
            comparison_path = run_dir / "research-provenance-comparison.json"
            comparison_path.write_text(json.dumps({"runId": run_dir.name, "stable": False}))
            with self.assertRaisesRegex(ValueError, "stable matching pre/post provenance"):
                rebuild.verify_profile_inputs(run_dir)

    def test_rebuild_refuses_an_existing_output_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory) / "profile-run"
            output_dir = Path(directory) / "existing-output"
            run_dir.mkdir()
            output_dir.mkdir()
            with self.assertRaisesRegex(ValueError, "refusing to overwrite"):
                rebuild.rebuild(run_dir, output_dir, "1.99.1")


if __name__ == "__main__":
    unittest.main()
