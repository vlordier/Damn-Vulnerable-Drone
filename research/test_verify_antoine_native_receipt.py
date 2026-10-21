from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from build_antoine_source_snapshot import add_stable_rust_path_remaps
from verify_antoine_native_receipt import verify_native_receipt


class AntoineNativeReceiptVerificationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.root = self.base / "snapshot" / "antoine"
        self.root.mkdir(parents=True)
        self.snapshot_root = self.root.parent
        self.receipt = self.snapshot_root / "antoine-source-build-receipt.json"
        self.receipt_tool = self.root / "scripts/antoine_sitl_build_receipt.py"
        self.receipt_tool.parent.mkdir()
        self.receipt_tool.write_text("", encoding="utf-8")

    def write_receipt(self, flags_hash: str) -> None:
        self.receipt.write_text(
            json.dumps(
                {"toolchain": {"build_environment_sha256": {"CARGO_ENCODED_RUSTFLAGS": flags_hash}}}
            ),
            encoding="utf-8",
        )

    def test_legacy_receipt_runs_without_adding_remap_flags(self) -> None:
        self.write_receipt("")
        with (
            patch.dict(os.environ, {}, clear=True),
            patch("verify_antoine_native_receipt.subprocess.run") as run,
        ):
            verify_native_receipt(self.root, self.snapshot_root, self.receipt)
        self.assertNotIn("CARGO_ENCODED_RUSTFLAGS", run.call_args.kwargs["env"])

    def test_remapped_receipt_reconstructs_the_recorded_flag_environment(self) -> None:
        environment = {}
        add_stable_rust_path_remaps(environment, self.snapshot_root)
        flags_hash = hashlib.sha256(environment["CARGO_ENCODED_RUSTFLAGS"].encode()).hexdigest()
        self.write_receipt(flags_hash)
        with (
            patch.dict(os.environ, {}, clear=True),
            patch("verify_antoine_native_receipt.subprocess.run") as run,
        ):
            verify_native_receipt(self.root, self.snapshot_root, self.receipt)
        self.assertEqual(
            run.call_args.kwargs["env"]["CARGO_ENCODED_RUSTFLAGS"],
            environment["CARGO_ENCODED_RUSTFLAGS"],
        )
        self.assertEqual(run.call_args.kwargs["env"]["PYTHONPATH"], str(self.root.resolve()))

    def test_unreconstructable_flags_fail_before_running_verifier(self) -> None:
        self.write_receipt("0" * 64)
        with (
            patch.dict(os.environ, {}, clear=True),
            patch("verify_antoine_native_receipt.subprocess.run") as run,
        ):
            with self.assertRaisesRegex(ValueError, "cannot reconstruct"):
                verify_native_receipt(self.root, self.snapshot_root, self.receipt)
        run.assert_not_called()

    def test_symlinked_receipt_is_rejected(self) -> None:
        self.write_receipt("")
        link = self.base / "receipt-link.json"
        link.symlink_to(self.receipt)
        with self.assertRaisesRegex(ValueError, "regular file"):
            verify_native_receipt(self.root, self.snapshot_root, link)


if __name__ == "__main__":
    unittest.main()
