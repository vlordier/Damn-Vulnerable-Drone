from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import build_antoine_source_snapshot as builder


def write_snapshot(snapshot: Path) -> None:
    files = []
    for relative, content in (
        ("antoine/sitl/Cargo.toml", b"[package]\nname='fixture'\n"),
        ("ew-wire-contract/Cargo.toml", b"[package]\nname='wire'\n"),
        ("antoine/scripts/antoine_sitl_build_receipt.py", b"# fixture receipt tool\n"),
    ):
        path = snapshot / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        files.append(
            {
                "path": relative,
                "sizeBytes": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
        )
    manifest = hashlib.sha256()
    for entry in files:
        manifest.update(entry["path"].encode())
        manifest.update(b"\0")
        manifest.update(entry["sha256"].encode("ascii"))
        manifest.update(b"\n")
    (snapshot / "antoine-source-snapshot.json").write_text(
        json.dumps(
            {
                "contract": "damn-vulnerable-drone.antoine-source-snapshot",
                "version": "1.1.0",
                "status": "verified",
                "snapshotManifestSha256": manifest.hexdigest(),
                "files": files,
            }
        )
        + "\n",
        encoding="utf-8",
    )


class AntoineSnapshotBuildTests(unittest.TestCase):
    def test_stable_rust_path_remaps_preserve_existing_encoded_flags(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "source snapshot"
            source.mkdir()
            environment = {
                "CARGO_HOME": str(Path(temporary) / "cargo cache"),
                "CARGO_ENCODED_RUSTFLAGS": "-C\x1fdebuginfo=0",
                "RUSTFLAGS": "-C debuginfo=1",
            }

            builder.add_stable_rust_path_remaps(environment, source)

            self.assertEqual(
                environment["CARGO_ENCODED_RUSTFLAGS"].split("\x1f"),
                [
                    "-C",
                    "debuginfo=0",
                    f"--remap-path-prefix={source.resolve()}=.",
                    f"--remap-path-prefix={Path(environment['CARGO_HOME']).resolve()}=/cargo",
                ],
            )

    def test_stable_rust_path_remaps_encode_source_root_with_spaces(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "source snapshot"
            source.mkdir()
            environment = {"CARGO_HOME": str(Path(temporary) / "cargo")}

            builder.add_stable_rust_path_remaps(environment, source)

            self.assertIn(
                f"--remap-path-prefix={source.resolve()}=.",
                environment["CARGO_ENCODED_RUSTFLAGS"].split("\x1f"),
            )

    def test_tampered_bundle_is_rejected_before_build_target_creation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = Path(temporary) / "run"
            snapshot = run_dir / "antoine-source-snapshot"
            snapshot.mkdir(parents=True)
            write_snapshot(snapshot)
            (snapshot / "antoine/sitl/Cargo.toml").write_text("tampered\n", encoding="utf-8")
            target = run_dir / "native-target"

            with self.assertRaisesRegex(ValueError, "source snapshot bytes do not match"):
                builder.build_snapshot(snapshot, target, "1.99.1")

            self.assertFalse(target.exists())

    def test_existing_build_target_is_never_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = Path(temporary) / "run"
            snapshot = run_dir / "antoine-source-snapshot"
            snapshot.mkdir(parents=True)
            write_snapshot(snapshot)
            target = run_dir / "native-target"
            target.mkdir()
            (target / "keep.txt").write_text("existing evidence", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "refusing to overwrite"):
                builder.build_snapshot(snapshot, target, "1.99.1")

            self.assertEqual((target / "keep.txt").read_text(encoding="utf-8"), "existing evidence")

    def test_build_commands_use_the_run_owned_cargo_target(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = Path(temporary) / "run"
            snapshot = run_dir / "antoine-source-snapshot"
            snapshot.mkdir(parents=True)
            write_snapshot(snapshot)
            target = run_dir / "native-target"
            commands = []

            def run(command, *, cwd, environment):
                commands.append(
                    (
                        command,
                        cwd,
                        environment["CARGO_TARGET_DIR"],
                        environment["CARGO_ENCODED_RUSTFLAGS"],
                    )
                )
                if command[0] == "uv" and command[5] == "build":
                    binary_dir = cwd / ".cargo-target/antoine-sitl-build-receipt/debug"
                    binary_dir.mkdir(parents=True)
                    for name in builder.BINARIES:
                        binary = binary_dir / name
                        binary.write_text("fixture binary", encoding="utf-8")
                        binary.chmod(0o700)

            with (
                mock.patch.object(builder, "seal", return_value={"commits": {"antoine": "a"}}),
                mock.patch.object(builder, "_run", side_effect=run),
            ):
                result = builder.build_snapshot(snapshot, target, "1.99.1")

            expected_target = (
                snapshot / "antoine/.cargo-target/antoine-sitl-build-receipt"
            ).resolve()
            self.assertTrue(commands)
            self.assertTrue(
                all(Path(command_target) == expected_target for _, _, command_target, _ in commands)
            )
            self.assertTrue(
                all(
                    f"--remap-path-prefix={snapshot.resolve()}=." in encoded_flags.split("\x1f")
                    for _, _, _, encoded_flags in commands
                )
            )
            self.assertEqual(result["sourceSnapshot"]["fileCount"], 3)


if __name__ == "__main__":
    unittest.main()
