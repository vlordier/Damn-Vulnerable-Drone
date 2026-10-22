from __future__ import annotations

import os
import re
import signal
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from time import sleep

from apply_qgroundcontrol_patch import apply_patch_safely
from check_qgroundcontrol_patch import verify_applied_patch, verify_binary_in_checkout

PATCH = Path(__file__).resolve().parent / "patches" / "qgroundcontrol-antoine.patch"
ROOT = PATCH.parent.parent.parent


class QGroundControlPatchContractTests(unittest.TestCase):
    def test_qgc_launcher_uses_profile_settings_and_runtime_paths(self) -> None:
        with TemporaryDirectory() as temporary:
            temp_root = Path(temporary)
            qgc_root = temp_root / "qgc checkout"
            app = (
                qgc_root
                / "build"
                / "Debug"
                / "QGroundControl.app"
                / "Contents"
                / "MacOS"
                / "QGroundControl"
            )
            app.parent.mkdir(parents=True)
            settings_dir = temp_root / "profile settings"
            settings_file = temp_root / "receipts" / "QGC settings.ini"
            runtime_dir = temp_root / "profile runtime"
            marker = temp_root / "qgc-started"
            observed_config = temp_root / "observed-xdg-config"
            observed_args = temp_root / "observed-args"
            app.write_text(
                "#!/bin/sh\n"
                'printf "%s\\n" "$XDG_CONFIG_HOME" > "$QGC_TEST_CONFIG_CAPTURE"\n'
                'printf "%s\\n" "$*" > "$QGC_TEST_ARGS_CAPTURE"\n'
                ': > "$QGC_TEST_MARKER"\n'
                "exec /bin/sleep 10\n",
                encoding="utf-8",
            )
            app.chmod(0o755)

            fake_bin = temp_root / "bin"
            fake_bin.mkdir()
            uv_stub = fake_bin / "uv"
            uv_stub.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            lsof_stub = fake_bin / "lsof"
            lsof_stub.write_text(
                "#!/bin/sh\n"
                'case "$*" in\n'
                '  *"-iUDP:${QGC_UDP_LISTEN_PORT}"*)\n'
                '    if [ -e "$QGC_TEST_MARKER" ]; then printf "QGroundControl\\n"; exit 0; fi\n'
                "    exit 1 ;;\n"
                '  *"-iUDP:${QGC_BRIDGE_PORT}"*) printf "bridge\\n"; exit 0 ;;\n'
                "esac\n"
                "exit 1\n",
                encoding="utf-8",
            )
            uv_stub.chmod(0o755)
            lsof_stub.chmod(0o755)

            env = os.environ.copy()
            env.update(
                {
                    "PATH": f"{fake_bin}:{env['PATH']}",
                    "ANTOINE_QGC_DIR": str(qgc_root),
                    "ANTOINE_QGC_BINARY": str(app),
                    "QGC_SETTINGS_DIR": str(settings_dir),
                    "QGC_SETTINGS_FILE": str(settings_file),
                    "QGC_RUNTIME_DIR": str(runtime_dir),
                    "QGC_LOG": str(runtime_dir / "launcher.log"),
                    "QGC_PID_FILE": str(runtime_dir / "launcher.pid"),
                    "QGC_UDP_LISTEN_PORT": "18150",
                    "QGC_BRIDGE_PORT": "18151",
                    "QGC_TEST_MARKER": str(marker),
                    "QGC_TEST_CONFIG_CAPTURE": str(observed_config),
                    "QGC_TEST_ARGS_CAPTURE": str(observed_args),
                }
            )
            result = subprocess.run(
                ["bash", str(ROOT / "scripts/run-qgc-antoine.sh")],
                env=env,
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            pid = int((runtime_dir / "launcher.pid").read_text(encoding="utf-8"))
            try:
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(
                    observed_config.read_text(encoding="utf-8").strip(), str(settings_dir)
                )
                self.assertEqual(
                    observed_args.read_text(encoding="utf-8").strip(), "--allow-multiple"
                )
                self.assertEqual(
                    settings_file.read_text(encoding="utf-8"),
                    "[AutoConnect]\nautoConnectUDP=true\nudpListenPort=18150\n"
                    "udpTargetHostIP=127.0.0.1\nudpTargetHostPort=18151\n",
                )
                self.assertTrue((runtime_dir / "launcher.log").is_file())
            finally:
                try:
                    os.kill(pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                for _ in range(50):
                    try:
                        os.kill(pid, 0)
                    except ProcessLookupError:
                        break
                    sleep(0.01)

    def test_profile_preflight_receipt_and_launcher_share_qgc_binary_override(self) -> None:
        makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
        profile_runner = (ROOT / "research/run-antoine-gazebo-mavlink.sh").read_text(
            encoding="utf-8"
        )
        qgc_launcher = (ROOT / "scripts/run-qgc-antoine.sh").read_text(encoding="utf-8")
        self.assertIn('check_qgroundcontrol_patch.py" \\', qgc_launcher)
        self.assertIn('--binary "${QGC_APP}"', qgc_launcher)
        self.assertIn('QGC_SETTINGS_FILE="${QGC_SETTINGS_FILE:-', qgc_launcher)
        self.assertIn('QGC_SETTINGS_FILE="${QGC_SETTINGS_FILE}"', profile_runner)
        self.assertIn("apply_qgroundcontrol_patch.py", makefile)
        self.assertNotIn(
            'git -C "$(ANTOINE_QGC_DIR)" apply "$(CURDIR)/$(QGC_ANTOINE_PATCH)"', makefile
        )
        self.assertIn(
            'QGC_SETTINGS_DIR="${QGC_SETTINGS_DIR:-${RUN_DIR}/qgc-settings}"', profile_runner
        )
        self.assertIn('QGC_RUNTIME_DIR="${QGC_RUNTIME_DIR}"', profile_runner)
        self.assertIn('QGC_RUNTIME_DIR="${QGC_RUNTIME_DIR:-/tmp/dvd-qgc-antoine}"', qgc_launcher)
        self.assertIn('QGC_LOG="${QGC_LOG:-${QGC_RUNTIME_DIR}/qgc.log}"', qgc_launcher)
        self.assertIn('QGC_PID_FILE="${QGC_PID_FILE:-${QGC_RUNTIME_DIR}/qgc.pid}"', qgc_launcher)
        self.assertIn('qgc_binary="$${ANTOINE_QGC_BINARY:-', makefile)
        self.assertIn('ANTOINE_QGC_BINARY="${QGC_BINARY}"', profile_runner)
        self.assertIn('QGC_APP="${ANTOINE_QGC_BINARY:-', qgc_launcher)
        self.assertIn('--binary "$$qgc_binary"', makefile)
        self.assertIn('--binary "${QGC_BINARY}"', profile_runner)

    def test_gazebo_launcher_validates_callsign_before_build_work(self) -> None:
        runner = (ROOT / "research/run-antoine-gazebo-mavlink.sh").read_text(encoding="utf-8")
        validation = runner.index('CALLSIGN}" =~ ^[A-Za-z0-9_-]{1,64}$')
        first_snapshot = runner.index("snapshot_antoine_build_inputs.py")
        self.assertLess(validation, first_snapshot)
        self.assertIn("one NATS subject token", runner[validation : validation + 250])

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

    def test_patch_application_requires_clean_checkout_and_is_idempotent(self) -> None:
        with TemporaryDirectory() as temporary:
            base = Path(temporary)
            patch = base / "antoine.patch"

            def create_checkout(name: str) -> tuple[Path, Path]:
                root = base / name
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
                patch.write_bytes(
                    subprocess.run(
                        ["git", "-C", str(root), "diff", "--", "sample.cc"],
                        check=True,
                        capture_output=True,
                    ).stdout
                )
                source.write_text("int limit = 4;\n", encoding="utf-8")
                return root, source

            clean_root, clean_source = create_checkout("clean")
            self.assertEqual(apply_patch_safely(clean_root, patch), "applied")
            self.assertEqual(clean_source.read_text(encoding="utf-8"), "int limit = 3;\n")
            self.assertEqual(apply_patch_safely(clean_root, patch), "already-applied")

            dirty_root, dirty_source = create_checkout("dirty")
            dirty_source.write_text("int limit = 5;\n", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "tracked changes"):
                apply_patch_safely(dirty_root, patch)
            self.assertEqual(dirty_source.read_text(encoding="utf-8"), "int limit = 5;\n")

            stale_root, stale_source = create_checkout("stale")
            stale_source.write_text("int limit = 5;\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(stale_root), "add", "sample.cc"], check=True)
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(stale_root),
                    "-c",
                    "user.name=Test",
                    "-c",
                    "user.email=test@example.invalid",
                    "commit",
                    "-qm",
                    "new baseline",
                ],
                check=True,
            )
            with self.assertRaisesRegex(RuntimeError, "does not apply cleanly"):
                apply_patch_safely(stale_root, patch)
            self.assertEqual(stale_source.read_text(encoding="utf-8"), "int limit = 5;\n")

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
