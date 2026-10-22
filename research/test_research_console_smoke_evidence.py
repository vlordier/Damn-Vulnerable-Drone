from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from flask import Flask
from simulator.mgmt.routes import main as management_routes

from build_evidence_manifest import build, write_manifest

ROOT = Path(__file__).resolve().parents[1]


class ResearchConsoleSmokeEvidenceTests(unittest.TestCase):
    def make_app(self, evidence_root: Path) -> Flask:
        app = Flask(
            __name__,
            root_path=str(ROOT / "simulator" / "mgmt"),
            template_folder=str(ROOT / "simulator" / "mgmt" / "templates"),
        )
        app.config.update(
            TESTING=True,
            RESEARCH_DIR=str(ROOT / "research"),
            RESEARCH_EVIDENCE_DIR=str(evidence_root),
        )
        app.register_blueprint(management_routes)
        return app

    def test_console_shows_smoke_correlation_and_serves_its_report(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            evidence_root = Path(directory)
            run_dir = evidence_root / "antoine-two-drone-smoke-20261022T120000Z-123"
            run_dir.mkdir()
            report = {
                "runId": run_dir.name,
                "status": "passed",
                "vehicles": {
                    "drone-1": {"received": True},
                    "drone-2": {"received": True},
                },
                "neutralActuatorStartupAttested": {"drone-1": True, "drone-2": True},
                "gazeboTruth": {"status": "observed", "separationAtLeast5m": True},
                "natsGazeboCorrelation": {
                    "status": "matched",
                    "vehicles": {
                        "drone-1": {
                            "matched": True,
                            "positionErrorM": 0.05,
                            "simulationTimeDeltaSeconds": 0.1,
                        },
                        "drone-2": {
                            "matched": True,
                            "positionErrorM": 0.08,
                            "simulationTimeDeltaSeconds": 0.2,
                        },
                    },
                },
                "natsStateSeparationAtLeast5m": True,
                "limits": "No controllers or flight were started.",
            }
            (run_dir / "research-two-drone-smoke.json").write_text(
                json.dumps(report), encoding="utf-8"
            )
            (run_dir / "research-provenance-comparison.json").write_text(
                json.dumps(
                    {
                        "runId": run_dir.name,
                        "prePhaseValid": True,
                        "postPhaseValid": True,
                        "profileMatches": True,
                        "stable": True,
                    }
                ),
                encoding="utf-8",
            )
            write_manifest(run_dir, build(run_dir))
            client = self.make_app(evidence_root).test_client()

            response = client.get("/attacks/research")
            self.assertEqual(response.status_code, 200)
            html = response.get_data(as_text=True)
            self.assertIn("Two-drone NATS/Gazebo wiring smoke passed", html)
            self.assertIn("NATS/Gazebo state correlation:", html)
            self.assertIn("<strong>matched</strong>", html)
            self.assertIn("drone-1: matched (0.05 m, 0.1 s)", html)

            download = client.get(
                f"/attacks/research/evidence/{run_dir.name}/research-two-drone-smoke.json"
            )
            self.assertEqual(download.status_code, 200)
            self.assertEqual(json.loads(download.get_data(as_text=True))["runId"], run_dir.name)
            download.close()

    def test_console_explains_startup_failure_and_serves_plain_text(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            evidence_root = Path(directory)
            run_dir = evidence_root / "antoine-two-drone-smoke-20261022T120001Z-124"
            run_dir.mkdir()
            (run_dir / "research-run-failure.txt").write_text(
                f"runId={run_dir.name}\nstage=start-gazebo\nexitCode=1\n", encoding="utf-8"
            )
            write_manifest(run_dir, build(run_dir))
            client = self.make_app(evidence_root).test_client()

            response = client.get("/attacks/research")
            self.assertEqual(response.status_code, 200)
            html = response.get_data(as_text=True)
            self.assertIn("Run failed during start-gazebo (exit 1)", html)
            self.assertIn("Run failure details", html)

            download = client.get(
                f"/attacks/research/evidence/{run_dir.name}/research-run-failure.txt"
            )
            self.assertEqual(download.status_code, 200)
            self.assertEqual(download.mimetype, "text/plain")
            self.assertIn("stage=start-gazebo", download.get_data(as_text=True))
            download.close()

    def test_console_does_not_present_tampered_failure_record_as_verified(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            evidence_root = Path(directory)
            run_dir = evidence_root / "antoine-two-drone-smoke-20261022T120002Z-125"
            run_dir.mkdir()
            failure_path = run_dir / "research-run-failure.txt"
            failure_path.write_text(
                f"runId={run_dir.name}\nstage=start-gazebo\nexitCode=1\n", encoding="utf-8"
            )
            write_manifest(run_dir, build(run_dir))
            failure_path.write_text(
                f"runId={run_dir.name}\nstage=forged\nexitCode=0\n", encoding="utf-8"
            )
            client = self.make_app(evidence_root).test_client()

            response = client.get("/attacks/research")
            self.assertEqual(response.status_code, 200)
            html = response.get_data(as_text=True)
            self.assertIn("Evidence manifest failed verification", html)
            self.assertNotIn("Run failure details", html)


if __name__ == "__main__":
    unittest.main()
