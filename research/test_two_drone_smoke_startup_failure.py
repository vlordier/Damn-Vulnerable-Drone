from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from dvd_research.evidence_contract import verify_manifest

ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "research" / "run-antoine-two-drone-smoke.sh"


class TwoDroneSmokeStartupFailureTests(unittest.TestCase):
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
