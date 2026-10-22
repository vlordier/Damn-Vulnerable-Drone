"""Tests for isolated and content-verified Antoine build input snapshots."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import snapshot_antoine_build_inputs as snapshot


def populate(root: Path, ew_wire: Path) -> None:
    for relative in snapshot.SOURCE_ROOTS:
        path = root / relative
        if relative.suffix:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("#!/bin/sh\n", encoding="utf-8")
        else:
            (path / "src").mkdir(parents=True)
            (path / "Cargo.toml").write_text("[package]\n", encoding="utf-8")
            (path / "src/lib.rs").write_text("pub fn value() {}\n", encoding="utf-8")
    (ew_wire / "src").mkdir(parents=True)
    (ew_wire / "Cargo.toml").write_text("[package]\n", encoding="utf-8")
    (ew_wire / "src/lib.rs").write_text("pub fn wire() {}\n", encoding="utf-8")


class AntoineSourceSnapshotTests(unittest.TestCase):
    def test_upstream_moved_grant_declaration_accepts_only_the_known_buffered_gate(self) -> None:
        hunk = (
            b"--- a/sitl/antoine-sitl/src/bin/antoine_sitl_nats.rs\n"
            b"+++ b/sitl/antoine-sitl/src/bin/antoine_sitl_nats.rs\n"
            b"@@ -1824,7 +1876,6 @@\n"
            b"         });\n"
            b"     }\n"
            b'     let grant_subject = format!("furia.prime.execution-grant.{callsign}");\n'
            b"-    let core_grant = Arc::new(core_lease::CoreLease::new());\n"
            b"     {\n"
            b'         let revoke_subject = format!("furia.prime.execution-revoke.{callsign}");\n'
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "sitl/antoine-sitl/src/bin/antoine_sitl_nats.rs"
            source.parent.mkdir(parents=True)
            source.write_text(
                "struct MissionSetpointBuffer;\n"
                "fn authorized_for() {}\n"
                "fn mission_setpoint_matches_live_authorization() {}\n"
                "buffer.authorized_for(&grants, &admissions, &callsign, &world_frame_id, now_ms)\n"
                "state=pending-authorization\n"
            )

            self.assertTrue(snapshot.patch_hunk_superseded_by_upstream(hunk, root))
            source.write_text(source.read_text().replace("struct MissionSetpointBuffer;\n", ""))
            self.assertFalse(snapshot.patch_hunk_superseded_by_upstream(hunk, root))

    def test_swarm_coalescing_marker_uses_the_rate_trace_enabled_by_the_runner(self) -> None:
        repository = Path(__file__).resolve().parent.parent
        patch = (repository / "research/patches/antoine-swarm-state-partition.patch").read_text(
            encoding="utf-8"
        )
        runner = (repository / "research/run-antoine-two-drone-flight.sh").read_text(
            encoding="utf-8"
        )

        self.assertIn("+                if rate_trace && coalesced > 0 {", patch)
        self.assertIn("ANTOINE_GZ_STATE_COALESCING_ENABLED callsign={state_callsign}", patch)
        self.assertNotIn("+                if trace && coalesced > 0 {", patch)
        self.assertIn("export ANTOINE_GZ_RATE_TRACE=1", runner)

    def test_local_path_contract_and_conformance_fixtures_are_frozen(self) -> None:
        from record_antoine_run_build import source_manifest
        from seal_antoine_source_snapshot import seal

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, wire, target = root / "source", root / "wire", root / "snapshot"
            populate(source, wire)
            (source / "sitl/antoine-sitl/Cargo.toml").write_text(
                '[package]\n[dependencies]\nsensor = { path = "../../../contracts/sensor/rust" }\n'
            )
            contract = root / "contracts/sensor"
            (contract / "rust/src").mkdir(parents=True)
            (contract / "fixtures").mkdir()
            (contract / "rust/Cargo.toml").write_text('[package]\nname="sensor"\n')
            (contract / "rust/src/lib.rs").write_text('pub const DOMAIN: &str = "v1";\n')
            (contract / "fixtures/vector.json").write_text('{"version":1}\n')
            receipt = snapshot.create_snapshot(source, wire, target)
            self.assertIn(
                "contracts/sensor/rust/src/lib.rs", {item["path"] for item in receipt["files"]}
            )
            self.assertTrue((target / "contracts/sensor/fixtures/vector.json").is_file())
            cache = root / "build"
            cache.mkdir()
            sealed = seal(target, cache)
            self.assertIn("contracts", sealed["commits"])
            self.assertIn(
                "contracts/sensor/rust/src/lib.rs",
                {item["path"] for item in source_manifest(target / "antoine")},
            )
            (target / "contracts/sensor/rust/src/lib.rs").write_text("tampered")
            with self.assertRaisesRegex(ValueError, "frozen snapshot"):
                source_manifest(target / "antoine")

    def test_source_patch_is_receipted_without_editing_the_checkout(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, wire, target = root / "source", root / "wire", root / "run/source"
            populate(source, wire)
            patch = root / "change.patch"
            patch.write_text(
                "--- a/sitl/antoine-sitl/src/lib.rs\n"
                "+++ b/sitl/antoine-sitl/src/lib.rs\n"
                "@@ -1 +1 @@\n-pub fn value() {}\n+pub fn improved() {}\n"
            )
            receipt = snapshot.create_snapshot(source, wire, target, source_patch=patch)
            self.assertEqual(receipt["sourcePatch"]["status"], "applied")
            self.assertEqual(receipt["sourcePatch"]["sha256"], snapshot.sha256(patch))
            self.assertNotEqual(receipt["sourceManifestSha256"], receipt["snapshotManifestSha256"])
            self.assertEqual(
                (source / "sitl/antoine-sitl/src/lib.rs").read_text(), "pub fn value() {}\n"
            )
            self.assertEqual(
                (target / "antoine/sitl/antoine-sitl/src/lib.rs").read_text(),
                "pub fn improved() {}\n",
            )
            second = snapshot.create_snapshot(
                target / "antoine", target / "ew-wire-contract", root / "second", source_patch=patch
            )
            self.assertEqual(second["sourcePatch"]["status"], "already-present")
            self.assertEqual(second["sourceManifestSha256"], second["snapshotManifestSha256"])

    def test_source_patch_rejects_traversal_missing_files_and_wrong_revision(self) -> None:
        for path in ("../outside", "/tmp/outside", "missing.rs", "sitl/antoine-sitl/src/lib.rs"):
            with self.subTest(path=path), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                source, wire, target = root / "source", root / "wire", root / "run/source"
                populate(source, wire)
                patch = root / "change.patch"
                patch.write_text(
                    f"--- a/{path}\n+++ b/{path}\n@@ -1 +1 @@\n-wrong revision\n+changed\n"
                )
                with self.assertRaises(ValueError):
                    snapshot.create_snapshot(source, wire, target, source_patch=patch)
                self.assertFalse(target.exists())

    def test_multiple_hunks_in_one_file_are_applied_and_verified_independently(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, wire, target = root / "source", root / "wire", root / "run/source"
            populate(source, wire)
            path = source / "sitl/antoine-sitl/src/lib.rs"
            path.write_text("pub fn first() {}\npub fn middle() {}\npub fn last() {}\n")
            patch = root / "multi-hunk.patch"
            patch.write_text(
                "diff --git a/sitl/antoine-sitl/src/lib.rs b/sitl/antoine-sitl/src/lib.rs\n"
                "--- a/sitl/antoine-sitl/src/lib.rs\n"
                "+++ b/sitl/antoine-sitl/src/lib.rs\n"
                "@@ -1 +1 @@\n-pub fn first() {}\n+pub fn first_changed() {}\n"
                "@@ -3 +3 @@\n-pub fn last() {}\n+pub fn last_changed() {}\n"
            )
            receipt = snapshot.create_snapshot(source, wire, target, source_patch=patch)
            self.assertEqual(receipt["sourcePatch"]["status"], "applied")
            self.assertEqual(
                (target / "antoine/sitl/antoine-sitl/src/lib.rs").read_text(),
                "pub fn first_changed() {}\npub fn middle() {}\npub fn last_changed() {}\n",
            )
            second = snapshot.create_snapshot(
                target / "antoine", target / "ew-wire-contract", root / "second", source_patch=patch
            )
            self.assertEqual(second["sourcePatch"]["status"], "already-present")

    def test_concatenated_patch_sections_keep_distinct_file_targets(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, wire, target = root / "source", root / "wire", root / "run/source"
            populate(source, wire)
            sitl_file = source / "sitl/antoine-sitl/src/lib.rs"
            runner_file = source / "scripts/gz_flight_proof.sh"
            sitl_file.write_text("pub fn value() {}\n", encoding="utf-8")
            runner_file.write_text("#!/bin/sh\nold_runner_gate=1\n", encoding="utf-8")
            patch = root / "combined.patch"
            patch.write_text(
                "diff --git a/sitl/antoine-sitl/src/lib.rs b/sitl/antoine-sitl/src/lib.rs\n"
                "--- a/sitl/antoine-sitl/src/lib.rs\n"
                "+++ b/sitl/antoine-sitl/src/lib.rs\n"
                "@@ -1 +1 @@\n-pub fn value() {}\n+pub fn improved() {}\n"
                "diff --git a/scripts/gz_flight_proof.sh b/scripts/gz_flight_proof.sh\n"
                "--- a/scripts/gz_flight_proof.sh\n"
                "+++ b/scripts/gz_flight_proof.sh\n"
                "@@ -2 +2 @@\n-old_runner_gate=1\n+new_runner_gate=1\n",
                encoding="utf-8",
            )

            receipt = snapshot.create_snapshot(source, wire, target, source_patch=patch)

            self.assertEqual(receipt["sourcePatch"]["status"], "applied")
            self.assertEqual(
                (target / "antoine/sitl/antoine-sitl/src/lib.rs").read_text(encoding="utf-8"),
                "pub fn improved() {}\n",
            )
            self.assertEqual(
                (target / "antoine/scripts/gz_flight_proof.sh").read_text(encoding="utf-8"),
                "#!/bin/sh\nnew_runner_gate=1\n",
            )

    def test_offset_patch_does_not_leave_unrecorded_backup_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, wire, target = root / "source", root / "wire", root / "run/source"
            populate(source, wire)
            (source / "sitl/antoine-sitl/src/lib.rs").write_text(
                "// added upstream\npub fn value() {}\n"
            )
            patch = root / "change.patch"
            patch.write_text(
                "--- a/sitl/antoine-sitl/src/lib.rs\n+++ b/sitl/antoine-sitl/src/lib.rs\n@@ -1 +1 @@\n-pub fn value() {}\n+pub fn improved() {}\n"
            )
            receipt = snapshot.create_snapshot(source, wire, target, source_patch=patch)
            self.assertEqual(receipt["sourcePatch"]["status"], "applied")
            self.assertFalse(list(target.rglob("*.orig")))
            self.assertEqual(
                (target / "antoine/sitl/antoine-sitl/src/lib.rs").read_text(),
                "// added upstream\npub fn improved() {}\n",
            )

    def test_already_present_patch_survives_upstream_context_changes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, wire, target = root / "source", root / "wire", root / "run/source"
            populate(source, wire)
            source_file = source / "sitl/antoine-sitl/src/lib.rs"
            source_file.write_text(
                "fn renamed_top() {}\npub fn improved() {}\nfn renamed_bottom() {}\n"
            )
            patch = root / "change.patch"
            patch.write_text(
                "--- a/sitl/antoine-sitl/src/lib.rs\n"
                "+++ b/sitl/antoine-sitl/src/lib.rs\n"
                "@@ -1,3 +1,3 @@\n"
                " fn top() {}\n"
                "-pub fn value() {}\n"
                "+pub fn improved() {}\n"
                " fn bottom() {}\n"
            )
            receipt = snapshot.create_snapshot(source, wire, target, source_patch=patch)
            self.assertEqual(receipt["sourcePatch"]["status"], "already-present")
            self.assertEqual(receipt["sourceManifestSha256"], receipt["snapshotManifestSha256"])

    def test_newer_buffered_grant_gate_supersedes_old_receive_time_hunks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "sitl/antoine-sitl/src/bin/antoine_sitl_nats.rs"
            target.parent.mkdir(parents=True)
            target.write_text(
                "struct MissionSetpointBuffer { candidate: bool }\n"
                "fn authorized_for() {}\n"
                "fn mission_setpoint_matches_live_authorization() {}\n"
                "buffer.authorized_for(&grants, &admissions, &callsign, &world_frame_id, now_ms);\n"
                "state=pending-authorization;\n",
                encoding="utf-8",
            )
            receiver_hunk = (
                b"--- a/sitl/antoine-sitl/src/bin/antoine_sitl_nats.rs\n"
                b"+++ b/sitl/antoine-sitl/src/bin/antoine_sitl_nats.rs\n"
                b"@@ -1,2 +1,3 @@\n"
                b" let setpoint_subject = String::new();\n"
                b"+let grant = core_grant.for_command().await;\n"
            )
            declaration_move_hunk = (
                b"--- a/sitl/antoine-sitl/src/bin/antoine_sitl_nats.rs\n"
                b"+++ b/sitl/antoine-sitl/src/bin/antoine_sitl_nats.rs\n"
                b"@@ -8 +8,0 @@\n"
                b"-let core_grant = Arc::new(core_lease::CoreLease::new());\n"
            )

            self.assertTrue(snapshot.patch_hunk_superseded_by_upstream(receiver_hunk, root))
            self.assertTrue(snapshot.patch_hunk_superseded_by_upstream(declaration_move_hunk, root))
            target.write_text(target.read_text().replace("fn authorized_for() {}\n", ""))
            self.assertFalse(snapshot.patch_hunk_superseded_by_upstream(receiver_hunk, root))

    def test_shifted_already_present_hunk_and_new_hunk_are_reconciled(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, wire, target = root / "source", root / "wire", root / "run/source"
            populate(source, wire)
            source_file = source / "sitl/antoine-sitl/src/lib.rs"
            source_file.write_text(
                "fn renamed_top() {}\npub fn improved() {}\nfn renamed_bottom() {}\npub fn extra() {}\n"
            )
            patch = root / "mixed.patch"
            patch.write_text(
                "diff --git a/sitl/antoine-sitl/src/lib.rs b/sitl/antoine-sitl/src/lib.rs\n"
                "--- a/sitl/antoine-sitl/src/lib.rs\n"
                "+++ b/sitl/antoine-sitl/src/lib.rs\n"
                "@@ -1,3 +1,3 @@\n"
                " fn top() {}\n"
                "-pub fn value() {}\n"
                "+pub fn improved() {}\n"
                " fn bottom() {}\n"
                "@@ -4 +4 @@\n"
                "-pub fn extra() {}\n"
                "+pub fn extra_improved() {}\n"
            )
            receipt = snapshot.create_snapshot(source, wire, target, source_patch=patch)
            self.assertEqual(receipt["sourcePatch"]["status"], "applied")
            self.assertEqual(
                (target / "antoine/sitl/antoine-sitl/src/lib.rs").read_text(),
                "fn renamed_top() {}\npub fn improved() {}\nfn renamed_bottom() {}\npub fn extra_improved() {}\n",
            )

    def test_snapshot_has_a_verified_manifest_and_stands_alone(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, wire, target = root / "source", root / "wire", root / "run/source"
            populate(source, wire)

            receipt = snapshot.create_snapshot(source, wire, target)

            self.assertEqual(receipt["status"], "verified")
            self.assertEqual(receipt["sourceManifestSha256"], receipt["snapshotManifestSha256"])
            self.assertTrue((target / "antoine/sitl/antoine-sitl/Cargo.toml").is_file())
            self.assertTrue((target / "ew-wire-contract/Cargo.toml").is_file())
            (source / "sitl/antoine-sitl/src/lib.rs").write_text("edited", encoding="utf-8")
            self.assertEqual(
                (target / "antoine/sitl/antoine-sitl/src/lib.rs").read_text(encoding="utf-8"),
                "pub fn value() {}\n",
            )

    def test_source_mutation_during_copy_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, wire, target = root / "source", root / "wire", root / "run/source"
            populate(source, wire)
            original_copy = snapshot.shutil.copyfile

            def copy_then_mutate(src: Path, dst: Path, *args: object, **kwargs: object) -> Path:
                result = original_copy(src, dst, *args, **kwargs)
                if Path(src).name == "lib.rs" and "antoine-sitl" in str(src):
                    Path(src).write_text("changed while copying", encoding="utf-8")
                return result

            with mock.patch.object(snapshot.shutil, "copyfile", side_effect=copy_then_mutate):
                with self.assertRaisesRegex(RuntimeError, "changed while"):
                    snapshot.create_snapshot(source, wire, target)
            self.assertFalse(target.exists())

    def test_rejects_symlinked_source_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, wire, target = root / "source", root / "wire", root / "run/source"
            populate(source, wire)
            module = source / "sitl/antoine-sitl/src/lib.rs"
            module.unlink()
            module.symlink_to(source / "sitl/antoine-sitl/Cargo.toml")
            with self.assertRaisesRegex(ValueError, "Symlink"):
                snapshot.create_snapshot(source, wire, target)
            self.assertFalse(target.exists())

    def test_versioned_lockfiles_override_ignored_external_locks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, wire, target = root / "source", root / "wire", root / "run/source"
            populate(source, wire)
            sitl_lock = root / "project-sitl.lock"
            boundary_lock = root / "project-boundary.lock"
            sitl_lock.write_text("sitl-lock-v1", encoding="utf-8")
            boundary_lock.write_text("boundary-lock-v1", encoding="utf-8")
            overrides = {
                "antoine/sitl/antoine-sitl/Cargo.lock": sitl_lock,
                "antoine/runtime/antoine-boundary/Cargo.lock": boundary_lock,
            }

            snapshot.create_snapshot(source, wire, target, overrides)

            self.assertEqual(
                (target / "antoine/sitl/antoine-sitl/Cargo.lock").read_text(encoding="utf-8"),
                "sitl-lock-v1",
            )
            self.assertEqual(
                (target / "antoine/runtime/antoine-boundary/Cargo.lock").read_text(
                    encoding="utf-8"
                ),
                "boundary-lock-v1",
            )


if __name__ == "__main__":
    unittest.main()
