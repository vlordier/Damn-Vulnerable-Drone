"""Tests for reproducible run-owned Antoine binary receipts."""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import record_antoine_run_build as receipt


class AntoineBuildReceiptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.antoine = self.root / "antoine"
        self.binaries = self.root / "bin"
        for source_root in (
            self.antoine / "sitl/antoine-sitl",
            self.antoine / "runtime/antoine-boundary",
            self.root / "ew-wire-contract",
        ):
            (source_root / "src").mkdir(parents=True)
            (source_root / "Cargo.toml").write_text("[package]\n", encoding="utf-8")
            (source_root / "src/lib.rs").write_text("pub fn value() {}\n", encoding="utf-8")
        (self.antoine / "sitl/antoine-sitl/Cargo.lock").write_text(
            "version = 3\n", encoding="utf-8"
        )
        self.binaries.mkdir()
        for name in receipt.BINARIES:
            binary = self.binaries / name
            binary.write_bytes(name.encode("utf-8"))
            binary.chmod(0o755)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_receipt_binds_source_toolchain_build_commands_and_copied_binaries(self) -> None:
        def fake_command(*command: str, **_kwargs: object) -> subprocess.CompletedProcess[str]:
            output = "abc123\n" if "rev-parse" in command else f"{command[0]} test-version\n"
            return subprocess.CompletedProcess(command, 0, output, "")

        with mock.patch.object(receipt.subprocess, "run", side_effect=fake_command):
            result = receipt.build_receipt(self.antoine, self.binaries, "run-123", "abc123")

        self.assertEqual(result["status"], "recorded")
        self.assertEqual(result["runId"], "run-123")
        self.assertEqual(result["sourceRevision"], "abc123")
        self.assertEqual(len(result["buildCommands"]), 2)
        self.assertEqual(
            {item["path"] for item in result["artifacts"]},
            {f"antoine-binaries/{name}" for name in receipt.BINARIES},
        )
        self.assertTrue(result["sourceManifestSha256"])

    def test_source_edit_changes_build_input_digest(self) -> None:
        def fake_command(*command: str, **_kwargs: object) -> subprocess.CompletedProcess[str]:
            output = "abc123\n" if "rev-parse" in command else "tool test-version\n"
            return subprocess.CompletedProcess(command, 0, output, "")

        with mock.patch.object(receipt.subprocess, "run", side_effect=fake_command):
            first = receipt.build_receipt(self.antoine, self.binaries, "run-123", "abc123")
            source = self.antoine / "sitl/antoine-sitl/src/lib.rs"
            source.write_text("pub fn changed() {}\n", encoding="utf-8")
            second = receipt.build_receipt(self.antoine, self.binaries, "run-123", "abc123")

        self.assertNotEqual(first["sourceManifestSha256"], second["sourceManifestSha256"])

    def test_missing_executable_artifact_is_rejected(self) -> None:
        (self.binaries / "antoine_gz_local_authority").unlink()
        with self.assertRaisesRegex(ValueError, "Missing or non-executable"):
            receipt.build_receipt(self.antoine, self.binaries, "run-123", "abc123")

    def test_native_commands_and_binary_digests_are_bound_to_the_run_receipt(self) -> None:
        native = {
            "build": {
                "target_dir": ".cargo-target/antoine-sitl-build-receipt",
                "command": [
                    "CARGO_TARGET_DIR=.cargo-target/antoine-sitl-build-receipt",
                    "cargo",
                    "build",
                    "--locked",
                ],
                "artifact_sha256": receipt.sha256(self.binaries / "antoine_sitl_nats"),
                "support_artifacts": {
                    name: {
                        "artifact_sha256": receipt.sha256(self.binaries / name),
                        "command": ["cargo", "build", "--locked", "--features", "service"],
                    }
                    for name in ("antoine_gz_local_authority", "antoine-boundary-service")
                },
            },
        }
        path = self.root / "native.json"
        path.write_text(json.dumps(native))
        with mock.patch.object(receipt, "command_version", return_value="test-tool"):
            result = receipt.build_receipt(self.antoine, self.binaries, "run-123", "abc123", path)
            self.assertEqual(result["buildCommands"][0], ["cargo", "build", "--locked"])
            self.assertEqual(result["nativeBuildReceiptSha256"], receipt.sha256(path))
            native["build"]["artifact_sha256"] = "0" * 64
            path.write_text(json.dumps(native))
            with self.assertRaisesRegex(ValueError, "differ from the native"):
                receipt.build_receipt(self.antoine, self.binaries, "run-123", "abc123", path)


if __name__ == "__main__":
    unittest.main()
