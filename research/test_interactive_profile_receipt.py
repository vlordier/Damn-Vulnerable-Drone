from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from create_interactive_profile_receipt import create_receipt, snapshot_record, source_record


class InteractiveReceiptTests(unittest.TestCase):
    @staticmethod
    def _snapshot_fixture(root: Path, contract: str) -> tuple[Path, Path, Path, str]:
        run_dir = root / "run-123"
        snapshot_dir = run_dir / "snapshot"
        snapshot_dir.mkdir(parents=True)
        data = b"x"
        (snapshot_dir / "input").write_bytes(data)
        digest = hashlib.sha256(data).hexdigest()
        manifest = hashlib.sha256(f"input\0{digest}\n".encode()).hexdigest()
        receipt = snapshot_dir / "snapshot.json"
        receipt.write_text(
            json.dumps(
                {
                    "contract": contract,
                    "version": "1.1.0",
                    "status": "verified",
                    "snapshotManifestSha256": manifest,
                    "files": [{"path": "input", "sizeBytes": 1, "sha256": digest}],
                }
            ),
            encoding="utf-8",
        )
        return run_dir, snapshot_dir, receipt, contract

    def test_snapshot_record_accepts_exact_v110_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_dir, snapshot, _receipt, contract = self._snapshot_fixture(
                Path(directory), "damn-vulnerable-drone.antoine-source-snapshot"
            )
            result = snapshot_record(run_dir, snapshot, "snapshot.json", contract)
            self.assertEqual(result["fileCount"], 1)

    def test_snapshot_record_rejects_duplicate_json_keys(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_dir, snapshot, receipt, contract = self._snapshot_fixture(
                Path(directory), "damn-vulnerable-drone.antoine-source-snapshot"
            )
            text = receipt.read_text(encoding="utf-8").replace(
                '"status": "verified"', '"status": "invalid", "status": "verified"'
            )
            receipt.write_text(text, encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "duplicate object key"):
                snapshot_record(run_dir, snapshot, "snapshot.json", contract)

    def test_snapshot_record_rejects_nonstandard_json_constants(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_dir, snapshot, receipt, contract = self._snapshot_fixture(
                Path(directory), "damn-vulnerable-drone.antoine-source-snapshot"
            )
            text = receipt.read_text(encoding="utf-8").replace('"sizeBytes": 1', '"sizeBytes": NaN')
            receipt.write_text(text, encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "non-standard JSON constant"):
                snapshot_record(run_dir, snapshot, "snapshot.json", contract)

    def test_snapshot_record_rejects_overflowing_json_numbers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_dir, snapshot, receipt, contract = self._snapshot_fixture(
                Path(directory), "damn-vulnerable-drone.antoine-source-snapshot"
            )
            text = receipt.read_text(encoding="utf-8").replace("{", '{"extra":1e999,', 1)
            receipt.write_text(text, encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "outside finite floating-point range"):
                snapshot_record(run_dir, snapshot, "snapshot.json", contract)

    def test_snapshot_record_rejects_boolean_file_size(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_dir, snapshot, receipt, contract = self._snapshot_fixture(
                Path(directory), "damn-vulnerable-drone.antoine-source-snapshot"
            )
            text = receipt.read_text(encoding="utf-8").replace(
                '"sizeBytes": 1', '"sizeBytes": true'
            )
            receipt.write_text(text, encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "bytes do not match"):
                snapshot_record(run_dir, snapshot, "snapshot.json", contract)

    def test_snapshot_record_rejects_unsupported_version(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_dir, snapshot, receipt, contract = self._snapshot_fixture(
                Path(directory), "damn-vulnerable-drone.antoine-source-snapshot"
            )
            text = receipt.read_text(encoding="utf-8").replace(
                '"version": "1.1.0"', '"version": "1.0.0"'
            )
            receipt.write_text(text, encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "receipt is incomplete"):
                snapshot_record(run_dir, snapshot, "snapshot.json", contract)

    def test_headless_gazebo_receipt_excludes_qgroundcontrol_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            antoine = root / "antoine"
            sim = root / "sim"
            antoine.mkdir()
            sim.mkdir()
            run_dir = root / "headless-run"
            run_dir.mkdir()
            artifacts = {}
            for name in (
                "bridge",
                "antoine_nats",
                "authority",
                "boundary",
                "gazebo",
                "qgc_patch",
                "world",
                "gui_config",
            ):
                path = root / name
                path.write_text(name, encoding="utf-8")
                artifacts[name] = path
            args = argparse.Namespace(
                run_dir=run_dir,
                antoine_root=antoine,
                sim_root=sim,
                qgc_root=None,
                qgc_binary=None,
                bridge_binary=artifacts["bridge"],
                antoine_nats_binary=artifacts["antoine_nats"],
                antoine_authority_binary=artifacts["authority"],
                antoine_boundary_binary=artifacts["boundary"],
                gazebo_binary=artifacts["gazebo"],
                qgc_patch=artifacts["qgc_patch"],
                runtime_world=artifacts["world"],
                qgc_settings=None,
                gui_config=artifacts["gui_config"],
                callsign="drone-1",
                nats_port=14238,
                qgc_port=14660,
                bridge_port=14661,
            )

            receipt = create_receipt(args)

            self.assertEqual(receipt["profile"], "headless-antoine-gazebo-mavlink")
            self.assertNotIn("qgroundcontrol", receipt["source"])
            self.assertIsNone(receipt["artifacts"]["qgroundcontrolBinarySha256"])
            self.assertIsNone(receipt["artifacts"]["qgcSettingsSha256"])
            runtime_config = (run_dir / "interactive-profile.json").read_text(encoding="utf-8")
            self.assertIn('"profile": "headless-antoine-gazebo-mavlink"', runtime_config)

    def test_receipt_rejects_invalid_callsign_before_writing_runtime_config(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory) / "run-123"
            run_dir.mkdir()
            args = argparse.Namespace(
                run_dir=run_dir,
                callsign="drone.one",
                nats_port=14238,
                qgc_port=14660,
                bridge_port=14661,
            )

            with self.assertRaisesRegex(ValueError, "one NATS subject token"):
                create_receipt(args)

            self.assertFalse((run_dir / "interactive-profile.json").exists())

    def test_receipt_rejects_invalid_or_duplicate_ports_before_writing_config(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory) / "run-123"
            run_dir.mkdir()
            args = argparse.Namespace(
                run_dir=run_dir,
                callsign="drone-1",
                nats_port=True,
                qgc_port=14660,
                bridge_port=14660,
            )

            with self.assertRaisesRegex(ValueError, "ports must be integers"):
                create_receipt(args)

            self.assertFalse((run_dir / "interactive-profile.json").exists())

            args.nats_port = 14238
            with self.assertRaisesRegex(ValueError, "UDP ports must be different"):
                create_receipt(args)

            self.assertFalse((run_dir / "interactive-profile.json").exists())

    def test_source_record_binds_tracked_and_untracked_dirty_content(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(["git", "init", str(root)], check=True, capture_output=True)
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(root),
                    "-c",
                    "user.name=Test",
                    "-c",
                    "user.email=test@example.invalid",
                    "commit",
                    "--allow-empty",
                    "-m",
                    "baseline",
                ],
                check=True,
                capture_output=True,
            )
            tracked = root / "tracked.txt"
            tracked.write_text("first", encoding="utf-8")
            subprocess.run(
                ["git", "-C", str(root), "add", "tracked.txt"], check=True, capture_output=True
            )
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(root),
                    "-c",
                    "user.name=Test",
                    "-c",
                    "user.email=test@example.invalid",
                    "commit",
                    "-m",
                    "tracked",
                ],
                check=True,
                capture_output=True,
            )

            clean = source_record("fixture", root)
            self.assertEqual(clean["classification"], "clean")

            tracked.write_text("changed", encoding="utf-8")
            untracked = root / "new file.txt"
            untracked.write_text("untracked one", encoding="utf-8")
            first = source_record("fixture", root)
            self.assertEqual(first["classification"], "dirty-unreviewed")
            self.assertEqual(first["dirtyFileCount"], 2)
            self.assertTrue(first["trackedDiffSha256"])
            self.assertNotIn("untracked one", str(first))

            untracked.write_text("untracked two", encoding="utf-8")
            second = source_record("fixture", root)
            self.assertNotEqual(first["dirtyFilesSha256"], second["dirtyFilesSha256"])

    def test_receipt_binds_run_ports_sources_and_generated_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = {}
            for name in (
                "antoine",
                "sim",
                "qgc",
                "qgc_binary",
                "bridge_binary",
                "antoine_nats_binary",
                "antoine_authority_binary",
                "antoine_boundary_binary",
                "gazebo_binary",
                "qgc_patch",
                "runtime_world",
                "qgc_settings",
                "gui_config",
            ):
                path = root / name
                if name in {"antoine", "sim", "qgc"}:
                    path.mkdir()
                else:
                    path.write_text(name, encoding="utf-8")
                paths[name] = path
            run_dir = root / "run-123"
            run_dir.mkdir()
            antoine_snapshot = run_dir / "antoine-source-snapshot"
            gazebo_snapshot = run_dir / "gazebo-source-snapshot"
            antoine_snapshot.mkdir()
            gazebo_snapshot.mkdir()
            antoine_file = antoine_snapshot / "antoine" / "Cargo.toml"
            antoine_file.parent.mkdir()
            antoine_bytes = b"[package]\nname = 'snapshot'\n"
            antoine_file.write_bytes(antoine_bytes)
            antoine_hash = hashlib.sha256(antoine_bytes).hexdigest()
            antoine_manifest = hashlib.sha256(
                f"antoine/Cargo.toml\0{antoine_hash}\n".encode()
            ).hexdigest()
            gazebo_file = gazebo_snapshot / "worlds" / "flight.sdf"
            gazebo_file.parent.mkdir()
            gazebo_bytes = b"<sdf/>\n"
            gazebo_file.write_bytes(gazebo_bytes)
            gazebo_hash = hashlib.sha256(gazebo_bytes).hexdigest()
            gazebo_manifest = hashlib.sha256(
                f"worlds/flight.sdf\0{gazebo_hash}\n".encode()
            ).hexdigest()
            (antoine_snapshot / "antoine-source-snapshot.json").write_text(
                json.dumps(
                    {
                        "contract": "damn-vulnerable-drone.antoine-source-snapshot",
                        "version": "1.1.0",
                        "status": "verified",
                        "snapshotManifestSha256": antoine_manifest,
                        "files": [
                            {
                                "path": "antoine/Cargo.toml",
                                "sizeBytes": len(antoine_bytes),
                                "sha256": antoine_hash,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            (gazebo_snapshot / "gazebo-source-snapshot.json").write_text(
                json.dumps(
                    {
                        "contract": "damn-vulnerable-drone.gazebo-source-snapshot",
                        "version": "1.1.0",
                        "status": "verified",
                        "snapshotManifestSha256": gazebo_manifest,
                        "files": [
                            {
                                "path": "worlds/flight.sdf",
                                "sizeBytes": len(gazebo_bytes),
                                "sha256": gazebo_hash,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            args = argparse.Namespace(
                run_dir=run_dir,
                antoine_root=paths["antoine"],
                sim_root=paths["sim"],
                qgc_root=paths["qgc"],
                qgc_binary=paths["qgc_binary"],
                bridge_binary=paths["bridge_binary"],
                antoine_nats_binary=paths["antoine_nats_binary"],
                antoine_authority_binary=paths["antoine_authority_binary"],
                antoine_boundary_binary=paths["antoine_boundary_binary"],
                gazebo_binary=paths["gazebo_binary"],
                qgc_patch=paths["qgc_patch"],
                runtime_world=paths["runtime_world"],
                qgc_settings=paths["qgc_settings"],
                gui_config=paths["gui_config"],
                callsign="drone-1",
                nats_port=14238,
                qgc_port=14660,
                bridge_port=14661,
                antoine_source_snapshot=antoine_snapshot,
                gazebo_source_snapshot=gazebo_snapshot,
            )
            with patch(
                "create_interactive_profile_receipt.source_record",
                side_effect=OSError("source inspection failed"),
            ):
                with self.assertRaisesRegex(OSError, "source inspection failed"):
                    create_receipt(args)
            self.assertFalse((run_dir / "interactive-profile.json").exists())

            receipt = create_receipt(args)
            self.assertEqual(receipt["runId"], "run-123")
            self.assertEqual(receipt["ports"]["nats"], 14238)
            self.assertEqual(receipt["callsign"], "drone-1")
            self.assertEqual(
                receipt["artifacts"]["bridgeBinarySha256"],
                hashlib.sha256(paths["bridge_binary"].read_bytes()).hexdigest(),
            )
            self.assertTrue(receipt["artifacts"]["runtimeConfigSha256"])
            self.assertEqual(
                receipt["artifacts"]["gazeboBinarySha256"],
                hashlib.sha256(paths["gazebo_binary"].read_bytes()).hexdigest(),
            )
            self.assertEqual(receipt["sourceSnapshots"]["antoine"]["fileCount"], 1)
            self.assertEqual(receipt["sourceSnapshots"]["gazebo"]["fileCount"], 1)
            self.assertEqual(
                receipt["sourceSnapshots"]["gazebo"]["snapshotManifestSha256"],
                gazebo_manifest,
            )
            self.assertIn("rustc", receipt["toolchain"])
            self.assertTrue((run_dir / "interactive-profile.json").is_file())

            gazebo_file.write_bytes(b"<sdf><mutated/></sdf>\n")
            with self.assertRaisesRegex(ValueError, "bytes do not match"):
                create_receipt(args)


if __name__ == "__main__":
    unittest.main()
