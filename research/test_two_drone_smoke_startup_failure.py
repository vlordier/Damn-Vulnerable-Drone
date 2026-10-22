from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from dvd_research.evidence_contract import verify_manifest
from dvd_research.run_nats import validate_run_nats_endpoint

ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "research" / "run-antoine-two-drone-smoke.sh"


class TwoDroneSmokeStartupFailureTests(unittest.TestCase):
    def _run_startup_fixture(
        self,
        fixture: Path,
        config_override: str = "",
        continue_to_gazebo: bool = False,
    ) -> Path:
        real_uv = shutil.which("uv")
        self.assertIsNotNone(real_uv)
        bin_dir = fixture / "bin"
        bin_dir.mkdir()
        sim_root = fixture / "sim"
        (sim_root / "worlds").mkdir(parents=True)
        (sim_root / "models" / "x500").mkdir(parents=True)
        (sim_root / "tools" / "gz-sitl-applier").mkdir(parents=True)
        (sim_root / "worlds" / "antoine-gz-flight-proof.sdf").write_text("world")
        (sim_root / "models" / "x500" / "model.sdf").write_text("model")
        applier = sim_root / "tools" / "gz-sitl-applier" / "gz-sitl-applier"
        applier.write_text("#!/bin/sh\nexit 0\n")
        applier.chmod(0o755)

        c2_scripts = fixture / "c2" / "scripts"
        c2_scripts.mkdir(parents=True)
        (fixture / "c2" / "requirements-gz-sitl.txt").write_text("")
        (c2_scripts / "c2-gz-sitl-bridge.py").write_text("pass\n")

        gz_root = fixture / "gz"
        (gz_root / "bin").mkdir(parents=True)
        (gz_root / "share" / "gz").mkdir(parents=True)
        (gz_root / "lib").mkdir()
        (gz_root / "setup.bash").write_text("")
        gz_bin = gz_root / "bin" / "gz"
        gz_bin.write_text(
            '#!/bin/sh\nif [ "$1" = topic ]; then if [ -n "${GZ_TOPIC_DELAY:-}" ]; then sleep "$GZ_TOPIC_DELAY"; fi; echo /other/topic; exit 0; fi\nsleep 30\n'
        )
        gz_bin.chmod(0o755)

        nats = bin_dir / "nats"
        nats.write_text("#!/bin/sh\nexit 0\n")
        nats.chmod(0o755)
        nats_server = bin_dir / "nats-server"
        if continue_to_gazebo:
            nats_server.write_text(
                "#!/bin/sh\n"
                'echo "Listening for client connections on 127.0.0.1:14599"\n'
                'echo "Server is ready"\n'
                "sleep 30\n"
            )
        else:
            nats_server.write_text("#!/bin/sh\nexit 0\n")
        nats_server.chmod(0o755)

        uv_wrapper = bin_dir / "uv"
        uv_wrapper.write_text(
            "#!/bin/sh\n"
            'case "$*" in\n'
            '  *"socket"*) echo 14599; exit 0 ;;\n'
            '  *"prepare_antoine_two_drone_world.py"*) printf world >"$6"; exit 0 ;;\n'
            f'  *"capture_antoine_provenance.py"*) exit {0 if continue_to_gazebo else 9} ;;\n'
            "esac\n"
            f'exec "{real_uv}" "$@"\n'
        )
        uv_wrapper.chmod(0o755)

        report_root = fixture / "runs"
        environment = os.environ.copy()
        environment.update(
            {
                "PATH": f"{bin_dir}{os.pathsep}{environment['PATH']}",
                "AGR_SIM": str(sim_root),
                "AGR_C2_SCRIPTS": str(c2_scripts),
                "GZ_BIN": str(gz_bin),
                "ANTOINE_REPORT_DIR": str(report_root),
            }
        )
        if config_override:
            environment["GZ_CONFIG_PATH"] = config_override
        else:
            environment.pop("GZ_CONFIG_PATH", None)
        if continue_to_gazebo:
            environment["ANTOINE_GZ_STARTUP_TIMEOUT_SECONDS"] = "1"
            environment["GZ_TOPIC_DELAY"] = "2.2"
        result = subprocess.run(
            ["bash", str(RUNNER)],
            cwd=ROOT,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 1 if continue_to_gazebo else 9, result.stderr)
        run_dirs = list(report_root.glob("antoine-two-drone-smoke-*"))
        self.assertEqual(len(run_dirs), 1)
        run_dir = run_dirs[0]
        if continue_to_gazebo:
            self.assertEqual(
                validate_run_nats_endpoint("nats://127.0.0.1:14599", run_dir, time.monotonic() + 1),
                14599,
            )
        failure = (run_dir / "research-run-failure.txt").read_text(encoding="utf-8")
        self.assertIn(
            "stage=start-gazebo" if continue_to_gazebo else "stage=preflight-provenance",
            failure,
        )
        return run_dir

    def _run_to_provenance_preflight(self, fixture: Path, config_override: str = "") -> Path:
        return self._run_startup_fixture(fixture, config_override)

    def test_gazebo_runtime_paths_are_derived_and_recorded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_dir = self._run_to_provenance_preflight(Path(directory))
            profile = (run_dir / "research-profile.env").read_text(encoding="utf-8")
            self.assertIn(f"GZ_CONFIG_PATH={Path(directory) / 'gz/share/gz'}\n", profile)
            self.assertIn(f"GZ_SIM_SYSTEM_PLUGIN_PATH={Path(directory) / 'gz/lib'}\n", profile)

    def test_gazebo_config_override_is_preserved_after_selected_install(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            override = str(Path(directory) / "caller-config")
            run_dir = self._run_to_provenance_preflight(Path(directory), override)
            profile = (run_dir / "research-profile.env").read_text(encoding="utf-8")
            self.assertIn(f"GZ_CONFIG_PATH={Path(directory) / 'gz/share/gz'}:{override}\n", profile)

    def test_gazebo_startup_timeout_preserves_topic_and_server_diagnostics(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_dir = self._run_startup_fixture(Path(directory), continue_to_gazebo=True)
            diagnostics = (run_dir / "gazebo-startup-diagnostics.txt").read_text(encoding="utf-8")
            self.assertIn("expected_topic=/world/antoine_gz_flight_proof/pose/info", diagnostics)
            self.assertIn("/other/topic", diagnostics)
            profile = (run_dir / "research-profile.env").read_text(encoding="utf-8")
            self.assertIn("ANTOINE_GZ_TOPIC_QUERY_TIMEOUT_SECONDS=5\n", profile)
            self.assertIn(
                "/other/topic",
                (run_dir / "gazebo-topic-list.txt").read_text(encoding="utf-8"),
            )
            manifest = json.loads(
                (run_dir / "research-evidence-manifest.json").read_text(encoding="utf-8")
            )
            self.assertIn(
                "gazebo-startup-diagnostics.txt",
                {item["name"] for item in manifest["publicEvidence"]},
            )
            self.assertIn(
                "gazebo-topic-list.txt",
                {item["name"] for item in manifest["publicEvidence"]},
            )

    def test_early_failure_is_preserved_in_a_verifiable_manifest(self) -> None:
        real_uv = shutil.which("uv")
        self.assertIsNotNone(real_uv)

        with tempfile.TemporaryDirectory() as directory:
            fixture = Path(directory)
            bin_dir = fixture / "bin"
            bin_dir.mkdir()
            sim_root = fixture / "sim"
            (sim_root / "worlds").mkdir(parents=True)
            (sim_root / "models" / "x500").mkdir(parents=True)
            (sim_root / "tools" / "gz-sitl-applier").mkdir(parents=True)
            (sim_root / "worlds" / "antoine-gz-flight-proof.sdf").write_text("world")
            (sim_root / "models" / "x500" / "model.sdf").write_text("model")
            applier = sim_root / "tools" / "gz-sitl-applier" / "gz-sitl-applier"
            applier.write_text("#!/bin/sh\nexit 0\n")
            applier.chmod(0o755)

            c2_scripts = fixture / "c2" / "scripts"
            c2_scripts.mkdir(parents=True)
            (fixture / "c2" / "requirements-gz-sitl.txt").write_text("")
            (c2_scripts / "c2-gz-sitl-bridge.py").write_text("pass\n")

            gz_root = fixture / "gz"
            (gz_root / "bin").mkdir(parents=True)
            (gz_root / "setup.bash").write_text("")
            gz_bin = gz_root / "bin" / "gz"
            gz_bin.write_text("#!/bin/sh\nexit 0\n")
            gz_bin.chmod(0o755)

            for tool in ("nats", "nats-server"):
                executable = bin_dir / tool
                executable.write_text("#!/bin/sh\nexit 0\n")
                executable.chmod(0o755)

            uv_wrapper = bin_dir / "uv"
            uv_wrapper.write_text(
                "#!/bin/sh\n"
                'case "$*" in\n'
                '  *"socket"*) echo 14599; exit 0 ;;\n'
                '  *"prepare_antoine_two_drone_world.py"*) exit 9 ;;\n'
                "esac\n"
                f'exec "{real_uv}" "$@"\n'
            )
            uv_wrapper.chmod(0o755)

            report_root = fixture / "runs"
            environment = os.environ.copy()
            environment.update(
                {
                    "PATH": f"{bin_dir}{os.pathsep}{environment['PATH']}",
                    "AGR_SIM": str(sim_root),
                    "AGR_C2_SCRIPTS": str(c2_scripts),
                    "GZ_BIN": str(gz_bin),
                    "ANTOINE_REPORT_DIR": str(report_root),
                }
            )
            result = subprocess.run(
                ["bash", str(RUNNER)],
                cwd=ROOT,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(result.returncode, 9, result.stderr)
            run_dirs = list(report_root.glob("antoine-two-drone-smoke-*"))
            self.assertEqual(len(run_dirs), 1)
            run_dir = run_dirs[0]
            failure = (run_dir / "research-run-failure.txt").read_text(encoding="utf-8")
            self.assertIn("stage=prepare-world", failure)
            self.assertIn("exitCode=9", failure)
            valid, errors = verify_manifest(run_dir / "research-evidence-manifest.json")
            self.assertTrue(valid, errors)
            manifest = json.loads(
                (run_dir / "research-evidence-manifest.json").read_text(encoding="utf-8")
            )
            self.assertIn(
                "research-run-failure.txt",
                {item["name"] for item in manifest["publicEvidence"]},
            )


if __name__ == "__main__":
    unittest.main()
