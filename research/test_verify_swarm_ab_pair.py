from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

from verify_swarm_ab_pair import (
    REQUIRED_ARTIFACTS,
    config_pair,
    verify_artifacts,
    verify_profile_config,
    verify_provenance,
)


def swarm_config() -> dict:
    return {
        "contract": "damn-vulnerable-drone.antoine-swarm-config",
        "version": "1.3.0",
        "behavior": "lanes",
        "spacingM": 8.0,
        "mavlinkSwarm": False,
        "fault": "none",
        "arrivalHoldRadiusM": 0.15,
        "terminalBrakingRadiusM": 0.15,
        "retainPredecessorGrant": True,
        "vehicles": [
            {"callsign": "drone-1", "model": "isr"},
            {"callsign": "drone-2", "model": "fpv-a"},
        ],
    }


def stable_provenance() -> dict:
    return {
        "status": "stable",
        "sourceTrees": {
            name: {
                "available": True,
                "revision": "deadbeef",
                "sourceContentSha256": "a" * 64,
            }
            for name in ("antoine", "gazebo_distribution", "gazebo_sim_assets", "dvd_research")
        },
        "executables": {
            name: {"available": True, "sha256": "a" * 64}
            for name in (
                "gazebo_binary",
                "source_world",
                "runtime_world_overlay",
                "x500_model",
                "antoine_sitl_binary",
                "antoine_build_receipt",
                "antoine_source_snapshot_receipt",
                "gazebo_source_snapshot_receipt",
                "gazebo_model_assets",
            )
        },
    }


class PairConfigTests(unittest.TestCase):
    def test_accepts_only_the_selected_radius_difference(self) -> None:
        baseline = swarm_config()
        variant = swarm_config()
        variant["arrivalHoldRadiusM"] = 0.75
        key, before, after, digest = config_pair(baseline, variant, "hold-radius")
        self.assertEqual(key, "arrivalHoldRadiusM")
        self.assertEqual((before, after), (0.15, 0.75))
        self.assertEqual(len(digest), 64)

    def test_rejects_an_unselected_config_change(self) -> None:
        baseline = swarm_config()
        variant = swarm_config()
        variant["arrivalHoldRadiusM"] = 0.75
        variant["spacingM"] = 10.0
        with self.assertRaisesRegex(ValueError, "outside the selected factor"):
            config_pair(baseline, variant, "hold-radius")

    def test_requires_selected_factor_to_change_and_retain_its_type(self) -> None:
        baseline = swarm_config()
        variant = swarm_config()
        with self.assertRaisesRegex(ValueError, "must change"):
            config_pair(baseline, variant, "predecessor-retention")
        variant["retainPredecessorGrant"] = 0
        with self.assertRaisesRegex(ValueError, "must change"):
            config_pair(baseline, variant, "predecessor-retention")

    def test_requires_clean_two_vehicle_non_mavlink_runs(self) -> None:
        baseline = swarm_config()
        variant = swarm_config()
        variant["terminalBrakingRadiusM"] = 0.75
        variant["fault"] = "state-stream-partition"
        baseline["fault"] = "state-stream-partition"
        with self.assertRaisesRegex(ValueError, "clean non-MAVLink"):
            config_pair(baseline, variant, "braking-radius")


class PairProvenanceTests(unittest.TestCase):
    def test_accepts_identical_complete_stable_input_inventory(self) -> None:
        provenance = stable_provenance()
        verify_provenance(provenance, provenance)

    def test_rejects_mismatched_or_incomplete_provenance(self) -> None:
        baseline = stable_provenance()
        variant = stable_provenance()
        variant["executables"]["antoine_sitl_binary"]["sha256"] = "b" * 64
        with self.assertRaisesRegex(ValueError, "differs between the pair"):
            verify_provenance(baseline, variant)
        baseline = stable_provenance()
        del baseline["executables"]["gazebo_binary"]
        with self.assertRaisesRegex(ValueError, "missing pinned executable"):
            verify_provenance(baseline, baseline)


class PairArtifactTests(unittest.TestCase):
    def test_checks_required_raw_artifacts_against_reported_hashes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory)
            names = [
                *REQUIRED_ARTIFACTS,
                "research-profile.env",
                "research-swarm-config.json",
                "research-swarm-plan.json",
                "flight-evidence-drone-1.json",
                "gz-flight-proof-report-drone-1.json",
                "logs/antoine-controller-drone-1.log",
            ]
            artifacts = {}
            for name in names:
                path = run_dir / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("raw evidence\n", encoding="utf-8")
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
                artifacts[name] = {"available": True, "sha256": digest}
            report = {"evidenceArtifacts": artifacts, "vehicles": {"drone-1": {}}}
            verify_artifacts(run_dir, report)
            (run_dir / "gazebo-pose.jsonl").write_text("altered\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "changed after report generation"):
                verify_artifacts(run_dir, report)

    def test_checks_runtime_profile_values_against_the_recorded_config(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory)
            config = swarm_config()
            (run_dir / "research-profile.env").write_text(
                "ANTOINE_GZ_ARRIVAL_HOLD_RADIUS_M=0.15\n"
                "ANTOINE_GZ_TERMINAL_BRAKING_RADIUS_M=0.75\n"
                "ANTOINE_GZ_RETAIN_PREDECESSOR_GRANT=1\n",
                encoding="utf-8",
            )
            config["terminalBrakingRadiusM"] = 0.75
            verify_profile_config(run_dir, config)
            config["retainPredecessorGrant"] = False
            with self.assertRaisesRegex(ValueError, "does not match swarm config"):
                verify_profile_config(run_dir, config)


if __name__ == "__main__":
    unittest.main()
