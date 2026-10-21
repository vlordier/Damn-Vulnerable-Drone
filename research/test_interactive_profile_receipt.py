from __future__ import annotations

import argparse
import hashlib
import subprocess
import tempfile
import unittest
from pathlib import Path

from create_interactive_profile_receipt import create_receipt, source_record


class InteractiveReceiptTests(unittest.TestCase):
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

            with self.assertRaisesRegex(ValueError, "single non-empty NATS subject token"):
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
            )
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
            self.assertIn("rustc", receipt["toolchain"])
            self.assertTrue((run_dir / "interactive-profile.json").is_file())


if __name__ == "__main__":
    unittest.main()
