from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from verify_antoine_snapshot_reuse import verify_reuse


def write_snapshot(snapshot: Path, content: bytes = b"source\n") -> None:
    path = snapshot / "antoine/sitl/Cargo.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    digest = hashlib.sha256(content).hexdigest()
    manifest = hashlib.sha256()
    manifest.update(b"antoine/sitl/Cargo.toml\0")
    manifest.update(digest.encode("ascii"))
    manifest.update(b"\n")
    (snapshot / "antoine-source-snapshot.json").write_text(
        json.dumps(
            {
                "contract": "damn-vulnerable-drone.antoine-source-snapshot",
                "version": "1.1.0",
                "status": "verified",
                "snapshotManifestSha256": manifest.hexdigest(),
                "files": [
                    {
                        "path": "antoine/sitl/Cargo.toml",
                        "sizeBytes": len(content),
                        "sha256": digest,
                    }
                ],
            }
        )
        + "\n",
        encoding="utf-8",
    )


class AntoineSnapshotReuseTests(unittest.TestCase):
    def test_matching_captured_and_prebuilt_bundles_verify(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            current = root / "run/antoine-source-snapshot"
            prebuilt = root / "rebuild/source-snapshot"
            write_snapshot(current)
            write_snapshot(prebuilt)

            result = verify_reuse(current, prebuilt)

            self.assertEqual(result["status"], "verified")
            self.assertEqual(result["fileCount"], 1)

    def test_different_prebuilt_bundle_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            current = root / "run/antoine-source-snapshot"
            prebuilt = root / "rebuild/source-snapshot"
            write_snapshot(current)
            write_snapshot(prebuilt, b"different source\n")

            with self.assertRaisesRegex(ValueError, "snapshot differs"):
                verify_reuse(current, prebuilt)

    def test_tampered_prebuilt_bundle_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            current = root / "run/antoine-source-snapshot"
            prebuilt = root / "rebuild/source-snapshot"
            write_snapshot(current)
            write_snapshot(prebuilt)
            (prebuilt / "antoine/sitl/Cargo.toml").write_text("tampered\n", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "bytes do not match"):
                verify_reuse(current, prebuilt)


if __name__ == "__main__":
    unittest.main()
