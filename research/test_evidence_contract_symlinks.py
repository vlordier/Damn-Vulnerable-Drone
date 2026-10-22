from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from build_evidence_manifest import build
from dvd_research.evidence_contract import verify_manifest


class EvidenceManifestSymlinkTests(unittest.TestCase):
    def test_builder_omits_allowlisted_file_under_symlinked_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory) / "run-a"
            run_dir.mkdir()
            private_dir = run_dir / "private"
            private_dir.mkdir()
            (private_dir / "actuator-output.jsonl").write_text("private audit\n")
            (run_dir / "applier-neutral").symlink_to(private_dir, target_is_directory=True)

            manifest = build(run_dir)

        names = {item["name"] for item in manifest["publicEvidence"]}
        self.assertNotIn("applier-neutral/actuator-output.jsonl", names)

    def test_verifier_rejects_allowlisted_file_under_symlinked_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory) / "run-a"
            run_dir.mkdir()
            private_dir = run_dir / "private"
            private_dir.mkdir()
            content = b"private audit\n"
            (private_dir / "actuator-output.jsonl").write_bytes(content)
            (run_dir / "applier-neutral").symlink_to(private_dir, target_is_directory=True)
            manifest_path = run_dir / "research-evidence-manifest.json"
            manifest_path.write_text(
                json.dumps(
                    {
                        "contract": "damn-vulnerable-drone.antoine-evidence-manifest",
                        "runId": run_dir.name,
                        "publicEvidence": [
                            {
                                "name": "applier-neutral/actuator-output.jsonl",
                                "sizeBytes": len(content),
                                "sha256": hashlib.sha256(content).hexdigest(),
                            }
                        ],
                    }
                )
            )

            valid, errors = verify_manifest(manifest_path)

        self.assertFalse(valid)
        self.assertTrue(any("evidence file is missing or escapes" in error for error in errors))


if __name__ == "__main__":
    unittest.main()
