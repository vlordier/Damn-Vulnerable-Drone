"""Tests for immutable run-owned Gazebo flight input snapshots."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import snapshot_gazebo_inputs as snapshot


def build_source(root: Path) -> None:
    (root / "worlds").mkdir(parents=True)
    (root / "models" / "x500" / "meshes").mkdir(parents=True)
    (root / "tools" / "gz-sitl-applier").mkdir(parents=True)
    (root / "scripts").mkdir(parents=True)
    (root / "worlds" / "antoine-gz-flight-proof.sdf").write_text("world-v1", encoding="utf-8")
    (root / "models" / "x500" / "model.sdf").write_text("model-v1", encoding="utf-8")
    (root / "models" / "x500" / "meshes" / "body.dae").write_text("mesh-v1", encoding="utf-8")
    applier = root / "tools" / "gz-sitl-applier" / "gz-sitl-applier"
    applier.write_text("binary-v1", encoding="utf-8")
    applier.chmod(0o755)
    (root / "scripts/build_gz_sitl_applier.py").write_text("# builder\n", encoding="utf-8")
    (root / "scripts/gazebo_environment.sh").write_text("# gazebo environment\n", encoding="utf-8")
    (root / "scripts/strict_json.py").write_text("# strict json\n", encoding="utf-8")


class GazeboSnapshotTests(unittest.TestCase):
    def test_builds_patched_applier_inside_snapshot_with_source_and_binary_receipts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, target = root / "source", root / "run"
            build_source(source)
            code = source / "tools/gz-sitl-applier/gz-sitl-applier.cc"
            code.write_text("old-source\n")
            (source / "scripts/gazebo_environment.sh").write_text("# environment\n")
            (source / "tools/gz-sitl-applier/build.sh").write_text(
                '#!/bin/sh\nset -eu\ncp tools/gz-sitl-applier/gz-sitl-applier.cc "$FURIA_GZ_APPLIER_BUILD_OUTPUT"\nchmod 700 "$FURIA_GZ_APPLIER_BUILD_OUTPUT"\n'
            )
            patch = root / "applier.patch"
            patch.write_text(
                "--- a/tools/gz-sitl-applier/gz-sitl-applier.cc\n"
                "+++ b/tools/gz-sitl-applier/gz-sitl-applier.cc\n"
                "@@ -1 +1 @@\n-old-source\n+new-source\n"
            )
            receipt = snapshot.create_snapshot(source, target, patch)
            self.assertEqual(code.read_text(), "old-source\n")
            self.assertEqual(
                (source / "tools/gz-sitl-applier/gz-sitl-applier").read_text(), "binary-v1"
            )
            binary = target / "tools/gz-sitl-applier/gz-sitl-applier"
            self.assertEqual(binary.read_text(), "new-source\n")
            self.assertEqual(receipt["applierBuild"]["binarySha256"], snapshot.sha256_file(binary))
            self.assertEqual(receipt["applierBuild"]["patchSha256"], snapshot.sha256_file(patch))
            self.assertEqual(receipt["files"], snapshot.file_manifest(target))
            self.assertNotEqual(receipt["sourceManifestSha256"], receipt["snapshotManifestSha256"])

    def test_rejects_applier_patch_path_escape_before_compiling(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, target = root / "source", root / "run"
            build_source(source)
            patch = root / "applier.patch"
            patch.write_text("--- a/../outside.cc\n+++ b/../outside.cc\n@@ -1 +1 @@\n-a\n+b\n")
            with self.assertRaises(ValueError):
                snapshot.create_snapshot(source, target, patch)
            self.assertFalse(target.exists())

    def test_snapshot_is_run_owned_and_records_every_runtime_input(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, target = root / "source", root / "run" / "gazebo-inputs"
            build_source(source)

            receipt = snapshot.create_snapshot(source, target)

            self.assertEqual(receipt["status"], "verified")
            self.assertEqual(receipt["sourceManifestSha256"], receipt["snapshotManifestSha256"])
            self.assertEqual(
                (target / "models" / "x500" / "model.sdf").read_text(encoding="utf-8"),
                "model-v1",
            )
            self.assertTrue(
                (target / "tools/gz-sitl-applier/gz-sitl-applier").stat().st_mode & 0o111
            )
            (source / "models" / "x500" / "model.sdf").write_text("model-v2", encoding="utf-8")
            self.assertEqual(
                (target / "models" / "x500" / "model.sdf").read_text(encoding="utf-8"),
                "model-v1",
            )

    def test_refuses_snapshot_if_source_changes_during_copy(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, target = root / "source", root / "run" / "gazebo-inputs"
            build_source(source)
            original_copy = snapshot.shutil.copyfile

            def copy_then_mutate(src: Path, dst: Path, *args: object, **kwargs: object) -> Path:
                result = original_copy(src, dst, *args, **kwargs)
                if Path(src).name == "model.sdf":
                    Path(src).write_text("changed-during-copy", encoding="utf-8")
                return result

            with mock.patch.object(snapshot.shutil, "copyfile", side_effect=copy_then_mutate):
                with self.assertRaisesRegex(RuntimeError, "changed while"):
                    snapshot.create_snapshot(source, target)
            self.assertFalse(target.exists())

    def test_rejects_symlinked_runtime_assets(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, target = root / "source", root / "run" / "gazebo-inputs"
            build_source(source)
            model = source / "models" / "x500" / "model.sdf"
            model.unlink()
            model.symlink_to(source / "worlds" / "antoine-gz-flight-proof.sdf")
            with self.assertRaisesRegex(ValueError, "Symlink"):
                snapshot.create_snapshot(source, target)
            self.assertFalse(target.exists())

    def test_refuses_to_overwrite_existing_destination(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, target = root / "source", root / "run" / "gazebo-inputs"
            build_source(source)
            target.mkdir(parents=True)
            with self.assertRaisesRegex(ValueError, "Refusing to overwrite"):
                snapshot.create_snapshot(source, target)


if __name__ == "__main__":
    unittest.main()
