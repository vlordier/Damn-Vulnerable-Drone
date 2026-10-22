from __future__ import annotations

import re
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from check_qgroundcontrol_patch import verify_applied_patch, verify_binary_in_checkout

PATCH = Path(__file__).resolve().parent / "patches" / "qgroundcontrol-antoine.patch"
ROOT = PATCH.parent.parent.parent


class QGroundControlPatchContractTests(unittest.TestCase):
    def test_profile_preflight_receipt_and_launcher_share_qgc_binary_override(self) -> None:
        makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
        profile_runner = (ROOT / "research/run-antoine-gazebo-mavlink.sh").read_text(
            encoding="utf-8"
        )
        qgc_launcher = (ROOT / "scripts/run-qgc-antoine.sh").read_text(encoding="utf-8")
        self.assertIn('QGC_SETTINGS_FILE="${QGC_SETTINGS_FILE:-', qgc_launcher)
        self.assertIn('QGC_SETTINGS_FILE="${QGC_SETTINGS_FILE}"', profile_runner)
        self.assertIn('qgc_binary="$${ANTOINE_QGC_BINARY:-', makefile)
        self.assertIn('ANTOINE_QGC_BINARY="${QGC_BINARY}"', profile_runner)
        self.assertIn('QGC_APP="${ANTOINE_QGC_BINARY:-', qgc_launcher)
        self.assertIn('--binary "$$qgc_binary"', makefile)
        self.assertIn('--binary "${QGC_BINARY}"', profile_runner)

    def test_antoine_arm_ui_is_scoped_to_supported_vehicle_identity(self) -> None:
        patch = PATCH.read_text(encoding="utf-8")
        self.assertIn(
            "_activeVehicle.vehicleType === MAVLinkEnums.MAV_TYPE_QUADROTOR &&",
            patch,
        )
        self.assertIn("_activeVehicle.id === 1", patch)
        self.assertIn("vehicle->id() == 1", patch)

    def test_guided_position_target_uses_radial_three_meter_limit(self) -> None:
        patch = PATCH.read_text(encoding="utf-8")
        match = re.search(r"^\+\s*if \((distance[^\n]+)\) \{$", patch, re.MULTILINE)
        self.assertIsNotNone(match, "guided-position safety guard is missing")
        guard = match.group(1)
        self.assertIn("distance > 3.0", guard)
        self.assertIn("relativeAltitude < 0.0", guard)
        self.assertIn("relativeAltitude > 3.0", guard)
        self.assertNotIn("qAbs(north)", guard)
        self.assertNotIn("qAbs(east)", guard)

    def test_patch_verifier_requires_the_entire_patch_to_be_applied(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary) / "qgc"
            root.mkdir()
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            source = root / "sample.cc"
            source.write_text("int limit = 4;\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(root), "add", "sample.cc"], check=True)
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
                    "-qm",
                    "baseline",
                ],
                check=True,
            )
            source.write_text("int limit = 3;\n", encoding="utf-8")
            patch = Path(temporary) / "patch.diff"
            patch.write_bytes(
                subprocess.run(
                    ["git", "-C", str(root), "diff", "--", "sample.cc"],
                    check=True,
                    capture_output=True,
                ).stdout
            )
            binary = root / "build" / "QGroundControl"
            binary.parent.mkdir()
            binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            binary.chmod(0o755)

            verify_binary_in_checkout(root, binary)
            verify_applied_patch(root, patch)
            source.write_text("int limit = 4;\n", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "not fully applied"):
                verify_applied_patch(root, patch)
            source.write_text("int limit = 2;\n", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "not fully applied"):
                verify_applied_patch(root, patch)

    def test_qgc_binary_override_must_remain_inside_its_checkout(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary) / "qgc"
            root.mkdir()
            outside = Path(temporary) / "QGroundControl"
            outside.write_text("not a QGC build", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "outside its source checkout"):
                verify_binary_in_checkout(root, outside)


if __name__ == "__main__":
    unittest.main()
