"""Native clean-checkout receipts operate only on verified task-owned bundles."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from capture_antoine_provenance import input_tree_digest
from seal_antoine_source_snapshot import archive, seal, verified_files
from snapshot_antoine_build_inputs import create_snapshot
from test_snapshot_antoine_build_inputs import populate


class SourceSealTests(unittest.TestCase):
    def test_clean_seal_and_source_only_archive_preserve_verified_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            populate(root / "source", root / "wire")
            snapshot = root / "snapshot"
            create_snapshot(root / "source", root / "wire", snapshot)
            target = root / "build-cache"
            target.mkdir()
            receipt = seal(snapshot, target)
            self.assertEqual(set(receipt["commits"]), {"antoine", "ew-wire-contract"})
            self.assertTrue(all(len(commit) == 40 for commit in receipt["commits"].values()))
            self.assertTrue(
                (snapshot / "antoine/.cargo-target/antoine-sitl-build-receipt").is_symlink()
            )
            self.assertTrue(
                input_tree_digest("antoine_build_source_snapshot", snapshot)["available"]
            )
            destination = root / "archive"
            archive(snapshot, destination)
            self.assertEqual(len(verified_files(destination)), len(verified_files(snapshot)))
            self.assertFalse((destination / "antoine/.git").exists())
            self.assertFalse((destination / "antoine/.cargo-target").exists())
            self.assertTrue((destination / "native-source-git.json").is_file())

    def test_tampered_source_cannot_be_sealed_or_archived(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            populate(root / "source", root / "wire")
            snapshot = root / "snapshot"
            create_snapshot(root / "source", root / "wire", snapshot)
            (snapshot / "antoine/sitl/antoine-sitl/src/lib.rs").write_text("tampered")
            target = root / "build-cache"
            target.mkdir()
            with self.assertRaisesRegex(ValueError, "bytes differ"):
                seal(snapshot, target)
            with self.assertRaisesRegex(ValueError, "bytes differ"):
                archive(snapshot, root / "archive")
            self.assertFalse((snapshot / "antoine/.git").exists())
            self.assertFalse((root / "archive").exists())

    def test_inherited_git_controls_cannot_redirect_snapshot_writes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            populate(root / "source", root / "wire")
            snapshot = root / "snapshot"
            create_snapshot(root / "source", root / "wire", snapshot)
            target = root / "build-cache"
            target.mkdir()
            with mock.patch.dict(
                os.environ, {"GIT_DIR": str(root / "foreign"), "GIT_WORK_TREE": str(root / "other")}
            ):
                seal(snapshot, target)
            self.assertTrue((snapshot / "antoine/.git").is_dir())
            self.assertFalse((root / "foreign").exists())

    def test_generated_metadata_is_excluded_only_for_the_source_input(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            (source / "controller.rs").write_text("source-v1")
            before = input_tree_digest("antoine_build_source_snapshot", source)
            (source / ".git").mkdir()
            (source / ".git/index").write_text("metadata")
            (source / ".cargo-target").symlink_to(root / "elsewhere", target_is_directory=True)
            after = input_tree_digest("antoine_build_source_snapshot", source)
            self.assertEqual(before, after)
            self.assertFalse(input_tree_digest("gazebo_model_assets", source)["available"])
            (source / "controller.rs").write_text("source-v2")
            self.assertNotEqual(
                before["sha256"],
                input_tree_digest("antoine_build_source_snapshot", source)["sha256"],
            )
