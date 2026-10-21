"""Safety checks for post-acceptance Antoine Cargo target compaction."""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

import compact_antoine_rebuild_target as compact
from record_antoine_run_build import BINARIES


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")


def fixture(root: Path) -> dict[str, str]:
    target = root / "cargo-target"
    binary_dir = target / "debug"
    binary_dir.mkdir(parents=True)
    hashes = {}
    for index, name in enumerate(BINARIES):
        binary = binary_dir / name
        binary.write_bytes(f"executable-{index}".encode())
        binary.chmod(0o755)
        hashes[name] = digest(binary)
    (binary_dir / "deps").mkdir()
    (binary_dir / "deps/large-intermediate").write_bytes(b"intermediate")

    native = {
        "build": {
            "artifact_sha256": hashes[BINARIES[0]],
            "support_artifacts": {
                "antoine_gz_local_authority": {"artifact_sha256": hashes[BINARIES[1]]},
                "antoine-boundary-service": {"artifact_sha256": hashes[BINARIES[2]]},
            },
        }
    }
    native_path = root / "source-snapshot/antoine-source-build-receipt.json"
    write_json(native_path, native)
    source_link = root / "source-snapshot/antoine/.cargo-target"
    source_link.mkdir(parents=True)
    (source_link / "antoine-sitl-build-receipt").symlink_to(target, target_is_directory=True)

    run_build_path = root / "antoine-run-build-receipt.json"
    write_json(
        run_build_path,
        {
            "contract": "damn-vulnerable-drone.antoine-run-build",
            "nativeBuildReceiptSha256": digest(native_path),
            "artifacts": [
                {"path": f"antoine-binaries/{name}", "sha256": hashes[name]} for name in BINARIES
            ],
        },
    )
    write_json(
        root / "rebuild-receipt.json",
        {
            "contract": "damn-vulnerable-drone.antoine-snapshot-rebuild",
            "nativeBuildReceiptSha256": digest(native_path),
            "runBuildReceiptSha256": digest(run_build_path),
        },
    )
    write_json(
        root / "acceptance-run/gazebo-mavlink-acceptance.json",
        {
            "schema": "dvd.gazebo-mavlink-flight-check.v1",
            "passed": True,
            "finalMavlinkState": {"armed": False},
        },
    )
    write_json(root / "acceptance-run/research-provenance-comparison.json", {"stable": True})
    write_json(
        root / "acceptance-run/interactive-profile-stop-result.json",
        {
            "status": "stopped",
            "results": {
                "antoineGazeboSupervisor": "already-stopped",
                "mavlinkBridge": "already-stopped",
                "natsPort": "closed",
                "bridgeUdpBindPort": "closed",
                "qgcUdpListenPort": "closed",
            },
        },
    )
    copied = root / "acceptance-run/flight-proof/antoine-binaries"
    copied.mkdir(parents=True)
    for name in BINARIES:
        (copied / name).write_bytes((binary_dir / name).read_bytes())
    return hashes


class CompactAntoineRebuildTargetTests(unittest.TestCase):
    def test_keeps_receipt_verified_binaries_and_removes_intermediates(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "rebuild"
            root.mkdir()
            hashes = fixture(root)

            result = compact.compact(root)

            self.assertEqual(result["status"], "compacted")
            self.assertFalse((root / "cargo-target/debug/deps").exists())
            for name in BINARIES:
                self.assertEqual(digest(root / "cargo-target/debug" / name), hashes[name])
                self.assertTrue((root / "cargo-target/debug" / name).stat().st_mode & 0o111)

    def test_failed_acceptance_preserves_original_target(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "rebuild"
            root.mkdir()
            fixture(root)
            acceptance_path = root / "acceptance-run/gazebo-mavlink-acceptance.json"
            acceptance = json.loads(acceptance_path.read_text())
            acceptance["passed"] = False
            write_json(acceptance_path, acceptance)

            with self.assertRaisesRegex(ValueError, "passing, finally disarmed"):
                compact.compact(root)

            self.assertTrue((root / "cargo-target/debug/deps/large-intermediate").is_file())

    def test_binary_hash_mismatch_preserves_original_target(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "rebuild"
            root.mkdir()
            fixture(root)
            (root / "cargo-target/debug" / BINARIES[0]).write_bytes(b"changed")

            with self.assertRaisesRegex(ValueError, "does not match receipt"):
                compact.compact(root)

            self.assertTrue((root / "cargo-target/debug/deps/large-intermediate").is_file())

    def test_incomplete_profile_stop_preserves_original_target(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "rebuild"
            root.mkdir()
            fixture(root)
            write_json(
                root / "acceptance-run/interactive-profile-stop-result.json",
                {
                    "status": "partial",
                    "results": {"mavlinkBridge": "identity-mismatch-not-signalled"},
                },
            )

            with self.assertRaisesRegex(ValueError, "completed identity-checked profile stop"):
                compact.compact(root)

            self.assertTrue((root / "cargo-target/debug/deps/large-intermediate").is_file())


if __name__ == "__main__":
    unittest.main()
