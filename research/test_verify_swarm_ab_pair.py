from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from verify_swarm_ab_pair import (
    REQUIRED_ARTIFACTS,
    config_pair,
    load_host_contexts,
    require_same_host,
    summarize_host_load_comparison,
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
                "research-host-context-pre.json",
                "research-host-context-during.jsonl",
                "research-host-context-post.json",
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

    def test_host_context_records_pre_during_and_post_load_on_the_same_machine(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory) / "run-a"
            run_dir.mkdir()
            for phase in ("pre", "post"):
                write = {
                    "contract": "damn-vulnerable-drone.swarm-host-context",
                    "version": "1.0.0",
                    "runId": run_dir.name,
                    "phase": phase,
                    "capturedAtUtc": "2026-10-22T00:00:00+00:00",
                    "platform": "macOS",
                    "machine": "arm64",
                    "hostId": "a" * 64,
                    "logicalCpuCount": 10,
                    "loadAverage": {"status": "captured", "values": [1.0, 2.0, 3.0]},
                    "disk": {"freeBytes": 10_000},
                    "processes": {
                        "status": "captured",
                        "topCpuProcesses": [],
                        "simulationAndBuildProcesses": [],
                    },
                    "memory": {"status": "captured"},
                }
                (run_dir / f"research-host-context-{phase}.json").write_text(
                    json.dumps(write), encoding="utf-8"
                )
            with (run_dir / "research-host-context-during.jsonl").open(
                "w", encoding="utf-8"
            ) as stream:
                for sequence in range(2):
                    stream.write(
                        json.dumps(
                            {
                                "contract": "damn-vulnerable-drone.swarm-host-context",
                                "version": "1.0.0",
                                "runId": run_dir.name,
                                "phase": "during",
                                "sampleSequence": sequence,
                                "capturedAtUtc": "2026-10-22T00:00:00+00:00",
                                "capturedAtUnixNs": 1_000_000_000 + sequence,
                                "hostId": "a" * 64,
                                "loadAverage": {
                                    "status": "captured",
                                    "values": [float(sequence), 2.0, 3.0],
                                },
                                "disk": {"freeBytes": 10_000 - sequence},
                                "processes": {
                                    "status": "captured",
                                    "topCpuProcesses": [],
                                    "simulationAndBuildProcesses": [],
                                },
                            }
                        )
                        + "\n"
                    )
            result = load_host_contexts(run_dir)
            self.assertEqual(result["pre"]["loadAverage"]["values"], [1.0, 2.0, 3.0])
            self.assertEqual(result["post"]["diskFreeBytes"], 10_000)
            self.assertEqual(result["during"]["sampleCount"], 2)
            self.assertEqual(result["during"]["maxLoadAverage"], [1.0, 2.0, 3.0])
            require_same_host(result, result)
            other_host = {**result, "post": {**result["post"], "hostId": "b" * 64}}
            with self.assertRaisesRegex(ValueError, "one host"):
                require_same_host(result, other_host)

    def test_summarizes_load_and_disk_deltas_without_claiming_causality(self) -> None:
        baseline = {
            "during": {
                "sampleCount": 12,
                "maxLoadAverage": [2.0, 3.0, 4.0],
                "minDiskFreeBytes": 9_000,
                "peakCpuPercentByProcess": {"gz": 75.0},
                "simulationAndBuildProcessNames": ["gz", "nats-server"],
            }
        }
        variant = {
            "during": {
                "sampleCount": 10,
                "maxLoadAverage": [3.25, 2.5, 5.0],
                "minDiskFreeBytes": 8_500,
                "peakCpuPercentByProcess": {"gz": 90.0, "cargo": 35.0},
                "simulationAndBuildProcessNames": ["cargo", "gz"],
            }
        }

        result = summarize_host_load_comparison(baseline, variant)

        self.assertEqual(result["sampleCounts"], {"baseline": 12, "variant": 10})
        self.assertEqual(result["maxLoadAverage"]["variantMinusBaseline"], [1.25, -0.5, 1.0])
        self.assertEqual(result["minimumDiskFreeBytes"]["variantMinusBaseline"], -500)
        self.assertIn("does not qualify equivalent load", result["interpretation"])

    def test_rejects_out_of_order_during_host_samples(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory) / "run-a"
            run_dir.mkdir()
            # Seed complete boundary snapshots; the live sample sequence itself is malformed.
            for phase in ("pre", "post"):
                (run_dir / f"research-host-context-{phase}.json").write_text(
                    json.dumps(
                        {
                            "contract": "damn-vulnerable-drone.swarm-host-context",
                            "version": "1.0.0",
                            "runId": run_dir.name,
                            "phase": phase,
                            "capturedAtUtc": "2026-10-22T00:00:00+00:00",
                            "platform": "macOS",
                            "machine": "arm64",
                            "hostId": "a" * 64,
                            "logicalCpuCount": 10,
                            "loadAverage": {"status": "captured", "values": [1, 2, 3]},
                            "disk": {"freeBytes": 10},
                            "processes": {
                                "status": "captured",
                                "topCpuProcesses": [],
                                "simulationAndBuildProcesses": [],
                            },
                        }
                    ),
                    encoding="utf-8",
                )
            sample = {
                "contract": "damn-vulnerable-drone.swarm-host-context",
                "version": "1.0.0",
                "runId": run_dir.name,
                "phase": "during",
                "sampleSequence": 1,
                "capturedAtUtc": "2026-10-22T00:00:00+00:00",
                "capturedAtUnixNs": 1,
                "hostId": "a" * 64,
                "loadAverage": {"status": "captured", "values": [1, 2, 3]},
                "disk": {"freeBytes": 10},
                "processes": {
                    "status": "captured",
                    "topCpuProcesses": [],
                    "simulationAndBuildProcesses": [],
                },
            }
            (run_dir / "research-host-context-during.jsonl").write_text(
                json.dumps(sample) + "\n", encoding="utf-8"
            )
            with self.assertRaisesRegex(ValueError, "during-run host context"):
                load_host_contexts(run_dir)

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
