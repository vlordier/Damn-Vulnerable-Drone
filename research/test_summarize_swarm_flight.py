"""Integration tests for the swarm flight summarizer and its plan binding."""

from __future__ import annotations

import hashlib
import io
import json
import math
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from lifecycle_test_fixtures import build_lifecycle
from summarize_antoine_two_drone_flight import (
    cross_vehicle_effect_windows_valid,
    load_swarm_config,
    load_swarm_plan,
    main,
    measure_pose_stream,
    selected_vehicle_authorization_evidence,
    synchronized_waypoint_snapshot,
)
from swarm_behavior import build_plan

CALLSIGNS = (("drone-1", "isr"), ("drone-2", "fpv-a"))


def write_json(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def mission_progress_fixture(
    run_id: str, callsign: str, waypoints: list[list[float]]
) -> list[dict]:
    events = []
    event_sequence = 0
    grant_sequence = 1
    setpoint_sequence = 1
    timestamp = 1000

    def append(event: str, waypoint_index: int, target: list[float]) -> None:
        nonlocal event_sequence, timestamp
        event_sequence += 1
        timestamp += 100
        events.append(
            {
                "event": event,
                "runId": run_id,
                "callsign": callsign,
                "eventSequence": event_sequence,
                "atUnixMs": timestamp,
                "waypointIndex": waypoint_index,
                "targetEnuM": target,
                "grantSequence": grant_sequence,
                "setpointSequence": setpoint_sequence,
                "vigieAdmissionSequence": 1,
            }
        )

    append("grant_issued", 0, waypoints[0])
    append("setpoint_published", 0, waypoints[0])
    for index, target in enumerate(waypoints[:-1]):
        event_sequence += 1
        timestamp += 100
        events.append(
            {
                "event": "waypoint_reached",
                "runId": run_id,
                "callsign": callsign,
                "eventSequence": event_sequence,
                "atUnixMs": timestamp,
                "simulationTimeNs": timestamp * 1_000_000,
                "waypointIndex": index,
                "advancingToWaypointIndex": index + 1,
                "reachedTargetEnuM": target,
                "nextTargetEnuM": waypoints[index + 1],
                "grantSequence": grant_sequence,
                "setpointSequence": setpoint_sequence,
                "vigieAdmissionSequence": 1,
            }
        )
        grant_sequence += 1
        append("grant_issued", index + 1, waypoints[index + 1])
        setpoint_sequence += 1
        append("setpoint_published", index + 1, waypoints[index + 1])
    return events


def build_run(
    run_dir: Path,
    behavior: str,
    *,
    offset_second_vehicle: float = 0.0,
    write_plan: bool = True,
    progress_events: bool = True,
) -> dict:
    run_dir.mkdir(parents=True)
    plan = build_plan(behavior, 2, 8.0, models=[model for _, model in CALLSIGNS])
    if write_plan:
        write_json(run_dir / "research-swarm-plan.json", plan)
    config = {
        "contract": "damn-vulnerable-drone.antoine-swarm-config",
        "version": "1.1.0",
        "behavior": behavior,
        "spacingM": 8.0,
        "mavlinkSwarm": False,
        "vehicles": [
            {
                "callsign": vehicle["callsign"],
                "model": vehicle["model"],
                "waypointsEnuM": vehicle["waypointsEnuM"],
                "finalSlotEnuM": vehicle["finalSlotEnuM"],
            }
            for vehicle in plan["vehicles"]
        ],
    }
    write_json(run_dir / "research-swarm-config.json", config)
    with (run_dir / "gazebo-pose.jsonl").open("w", encoding="utf-8") as stream:
        for index, vehicle in enumerate(plan["vehicles"]):
            first, last = vehicle["spawnEnuM"], vehicle["finalSlotEnuM"]
            if index == 1:
                last = [last[0] + offset_second_vehicle, last[1], last[2]]
            for step in range(21):
                fraction = step / 20
                stamp = 1.0 + step * 0.1
                position = [
                    first[axis] + (last[axis] - first[axis]) * fraction for axis in range(3)
                ]
                timestamp_ns = round(stamp * 1e9)
                seconds, nanoseconds = divmod(timestamp_ns, 1_000_000_000)
                stream.write(
                    json.dumps(
                        {
                            "header": {"stamp": {"sec": seconds, "nsec": nanoseconds}},
                            "pose": [
                                {
                                    "name": vehicle["model"],
                                    "position": {
                                        "x": position[0],
                                        "y": position[1],
                                        "z": position[2],
                                    },
                                    "orientation": {"x": 0.0, "y": 0.0, "z": 0.0, "w": 1.0},
                                }
                            ],
                        }
                    )
                    + "\n"
                )
    for vehicle in plan["vehicles"]:
        callsign = vehicle["callsign"]
        progress = (
            mission_progress_fixture(run_dir.name, callsign, vehicle["waypointsEnuM"])
            if progress_events
            else []
        )
        write_json(
            run_dir / f"gz-flight-proof-report-{callsign}.json",
            {
                "localGrant": {"runId": run_dir.name, "vehicleId": callsign},
                "timingReference": {
                    "source": "first-valid-gazebo-pose",
                    "simulationTimeOriginSeconds": 0.0,
                },
                "measured": {
                    "allLegsReached": True,
                    "maxHorizontalTravelM": 8.0,
                    "peakAltitudeM": 10.0,
                    "legs": [
                        {
                            "index": index,
                            "reached": True,
                            "kinematicArrivalPassed": True,
                            "simSeconds": 2.6 + index * 0.4,
                        }
                        for index, _waypoint in enumerate(vehicle["waypointsEnuM"])
                    ],
                },
                "poseTimelinePassed": True,
                "actuatorNeutralizationPassed": True,
                "localFlightPassed": True,
            },
        )
        write_json(
            run_dir / f"flight-evidence-{callsign}.json",
            {
                "callsign": callsign,
                "runId": run_dir.name,
                "telemetry": [
                    {
                        "runId": run_dir.name,
                        "identity": callsign,
                        "source": "antoine-gz-plant",
                        "observedAtUnixMs": 1000 + index,
                        "executionGrantActive": False,
                        "control_authority": "safe-hold",
                        "configuredControlRateHz": 25.0,
                        "observedStateRateHz": 25.0,
                        "ticksPerStateSample": 1.0,
                        "stateSamplesObserved": 64 + index,
                    }
                    for index in range(21)
                ],
                "missionProgress": progress,
                "stopEvents": [
                    {
                        "event": "observed-stop",
                        "runId": run_dir.name,
                        "callsign": callsign,
                        "eventSequence": 1,
                        "atUnixMs": 9_000,
                        "clockSource": "authority-process-monotonic",
                        "atMonotonicMs": 4_000,
                        "actuatorHoldStartedAtMonotonicMs": 4_000,
                        "stopSequence": 17,
                        "stopCommandId": f"{callsign}:17",
                        "reason": "mission-setpoint-missing-stale-or-mismatched",
                        "controllerDecision": "publish-fail-closed-safe-stop",
                        "reasonClass": "mission-setpoint-mismatch",
                        "grantSequence": 2,
                        "setpointSequence": 2,
                        "vigieAdmissionSequence": 2,
                        "controllerHoldDurationMsAtPublication": 20,
                    },
                    {
                        "event": "recovered",
                        "runId": run_dir.name,
                        "callsign": callsign,
                        "eventSequence": 2,
                        "atUnixMs": 10_001,
                        "clockSource": "authority-process-monotonic",
                        "atMonotonicMs": 5_001,
                        "holdStartedAtMonotonicMs": 4_000,
                        "stopSequence": 17,
                        "holdDurationMs": 1_001,
                        "recoveryActuatorSequence": 42,
                        "recoveryGrantSequence": 2,
                    },
                ],
            },
        )
        logs_dir = run_dir / "logs"
        logs_dir.mkdir(exist_ok=True)
        (logs_dir / f"antoine-controller-{callsign}.log").write_text(
            "".join(
                f"ANTOINE_GZ_VETO_RESULT callsign={callsign} vigie_correlation=test-{callsign}-{event['vigieAdmissionSequence']} vigie_sequence={event['vigieAdmissionSequence']} decision=execute veto_ms=0.1\n"
                for event in progress
                if event["event"] == "setpoint_published"
            ),
            encoding="utf-8",
        )
    (run_dir / "applier-neutral").mkdir()
    (run_dir / "applier-neutral" / "actuator-output.jsonl").write_text(
        "".join(
            [
                '{"runId":"' + run_dir.name + '","status":"neutral"}\n',
                *[
                    json.dumps(
                        {
                            "runId": run_dir.name,
                            "vehicleId": vehicle["callsign"],
                            "stage": "explicit-stop",
                            "authority": "safe-stop",
                            "neutral": True,
                            "recordedAtUnixMs": 9_001,
                            "sequence": 17,
                            "commandId": f"{vehicle['callsign']}:17",
                        }
                    )
                    + "\n"
                    for vehicle in plan["vehicles"]
                ],
                *[
                    json.dumps(
                        {
                            "runId": run_dir.name,
                            "vehicleId": vehicle["callsign"],
                            "stage": "authorized-command",
                            "authority": "verified-core-grant",
                            "recordedAtUnixMs": 10_000,
                            "sequence": 42,
                            "executionGrant": {
                                "grantSequence": grant_sequence,
                                "targetEnuMm": [round(value * 1000) for value in target],
                            },
                        }
                    )
                    + "\n"
                    for vehicle in plan["vehicles"]
                    for grant_sequence, target in enumerate(vehicle["waypointsEnuM"][1:], start=2)
                ],
            ]
        ),
        encoding="utf-8",
    )
    return plan


def run_summary(run_dir: Path) -> dict:
    with (
        mock.patch.object(sys, "argv", ["summarize", str(run_dir)]),
        redirect_stdout(io.StringIO()),
    ):
        exit_code = main()
    result = json.loads((run_dir / "research-swarm-flight.json").read_text())
    return {**result, "exitCode": exit_code}


def qualify_fixture_baseline(run_dir: Path) -> None:
    comparison = {
        "runId": run_dir.name,
        "prePhaseValid": True,
        "postPhaseValid": True,
        "stable": True,
    }
    write_json(run_dir / "research-provenance-comparison.json", comparison)
    binary_root = run_dir / "antoine-binaries"
    binary_root.mkdir()
    binary_hashes = {}
    for name in (
        "antoine_sitl_nats",
        "antoine_gz_local_authority",
        "antoine-boundary-service",
    ):
        binary = binary_root / name
        binary.write_bytes(name.encode("utf-8"))
        binary_hashes[name] = hashlib.sha256(binary.read_bytes()).hexdigest()
    launches = ["role\tpid\trelativeBinary\tsha256\n"]
    pid = 100
    for callsign in ("drone-1", "drone-2"):
        for role, binary in (
            ("boundary", "antoine-boundary-service"),
            ("controller", "antoine_sitl_nats"),
            ("local-authority", "antoine_gz_local_authority"),
        ):
            launches.append(
                f"{role}-{callsign}\t{pid}\tantoine-binaries/{binary}\t{binary_hashes[binary]}\n"
            )
            pid += 1
    (run_dir / "research-antoine-process-launches.tsv").write_text(
        "".join(launches), encoding="utf-8"
    )
    artifacts = [
        {
            "path": f"antoine-binaries/{name}",
            "sha256": digest,
            "sizeBytes": (binary_root / name).stat().st_size,
        }
        for name, digest in binary_hashes.items()
    ]
    write_json(
        run_dir / "research-antoine-build-receipt.json",
        {"runId": run_dir.name, "status": "recorded", "artifacts": artifacts},
    )
    gazebo_snapshot = run_dir / "gazebo-source-snapshot"
    gazebo_snapshot.mkdir()
    write_json(gazebo_snapshot / "gazebo-source-snapshot.json", {"status": "verified"})
    write_json(run_dir / "research-gazebo-source-snapshot.json", {"status": "verified"})
    antoine_snapshot = run_dir / "antoine-source-snapshot"
    antoine_snapshot.mkdir()
    write_json(antoine_snapshot / "antoine-source-snapshot.json", {"status": "verified"})
    input_names = (
        "gazebo_model_assets",
        "gazebo_source_snapshot_receipt",
        "antoine_build_source_snapshot",
        "antoine_source_snapshot_receipt",
        "antoine_build_receipt",
        "antoine_sitl_binary",
        "antoine_local_authority_binary",
        "antoine_boundary_binary",
    )
    write_json(
        run_dir / "research-provenance.json",
        {
            "sourceTrees": [],
            "inputs": [
                {"name": name, "available": True, "sha256": "a" * 64} for name in input_names
            ],
        },
    )
    actuator_output = run_dir / "applier-neutral" / "actuator-output.jsonl"
    actuator_output.parent.mkdir(exist_ok=True)
    with actuator_output.open("a", encoding="utf-8") as stream:
        stream.write('{"sample":1}\n')
    for callsign in ("drone-1", "drone-2"):
        report_path = run_dir / f"gz-flight-proof-report-{callsign}.json"
        report = json.loads(report_path.read_text(encoding="utf-8"))
        report["finalDisarmed"] = True
        report["measured"].update({"stopCommands": 1, "applierNeutralRows": 1})
        write_json(report_path, report)
        evidence_path = run_dir / f"flight-evidence-{callsign}.json"
        evidence = json.loads(evidence_path.read_text())
        lifecycle, _ = build_lifecycle(run_dir, callsign)
        evidence.update(lifecycle)
        write_json(evidence_path, evidence)


class ConfigLoadingTests(unittest.TestCase):
    def test_rejects_duplicate_models(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory)
            write_json(
                run_dir / "research-swarm-config.json",
                {
                    "spacingM": 8,
                    "behavior": "lanes",
                    "vehicles": [
                        {"callsign": "drone-1", "model": "isr"},
                        {"callsign": "drone-2", "model": "isr"},
                    ],
                },
            )
            with self.assertRaises(ValueError):
                load_swarm_config(run_dir)

    def test_missing_plan_falls_back_to_documented_lanes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory)
            write_json(
                run_dir / "research-swarm-config.json",
                {
                    "spacingM": 8,
                    "behavior": "lanes",
                    "vehicles": [
                        {"callsign": "drone-1", "model": "isr"},
                        {"callsign": "drone-2", "model": "fpv-a"},
                    ],
                },
            )
            config = load_swarm_config(run_dir)
            plan = load_swarm_plan(run_dir, config, config["vehicles"])
            self.assertEqual(plan["planSource"], "inferred-lanes-from-config")
            self.assertEqual(plan["behavior"], "lanes")

    def test_plan_must_match_the_configured_roster(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory)
            write_json(
                run_dir / "research-swarm-config.json",
                {
                    "spacingM": 8,
                    "behavior": "vee",
                    "vehicles": [
                        {"callsign": "drone-1", "model": "isr"},
                        {"callsign": "drone-2", "model": "fpv-a"},
                    ],
                },
            )
            mismatched_plan = build_plan("vee", 2, 8.0, models=["fpv-b", "fpv-c"])
            mismatched_plan["vehicles"][1]["callsign"] = "drone-3"
            write_json(run_dir / "research-swarm-plan.json", mismatched_plan)
            config = load_swarm_config(run_dir)
            with self.assertRaises(ValueError):
                load_swarm_plan(run_dir, config, config["vehicles"])

    def test_plan_must_match_the_configured_models(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory)
            write_json(
                run_dir / "research-swarm-config.json",
                {
                    "spacingM": 8,
                    "behavior": "vee",
                    "vehicles": [
                        {"callsign": "drone-1", "model": "isr"},
                        {"callsign": "drone-2", "model": "fpv-a"},
                    ],
                },
            )
            write_json(
                run_dir / "research-swarm-plan.json",
                build_plan("vee", 2, 8.0, models=["isr", "fpv-b"]),
            )
            config = load_swarm_config(run_dir)
            with self.assertRaises(ValueError):
                load_swarm_plan(run_dir, config, config["vehicles"])


class SummaryTests(unittest.TestCase):
    def test_selected_vehicle_control_uses_independent_world_clock_with_authority_sequence(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory) / "antoine-swarm-flight-clock-test"
            build_run(run_dir, "lanes")
            plan_path = run_dir / "research-swarm-plan.json"
            plan = json.loads(plan_path.read_text())
            selected = plan["vehicles"][1]
            previous = selected["waypointsEnuM"][-1]
            command = [10.0, 8.0, 10.0]
            selected["waypointsEnuM"].append(command)
            selected["finalSlotEnuM"] = command
            plan["selectedVehicleAuthorization"] = {
                "callsign": "drone-2",
                "previousWaypointEnuM": previous,
                "commandWaypointEnuM": command,
                "authorizationPath": "normal-run-local-authority-signed-grant",
            }
            write_json(plan_path, plan)
            events = mission_progress_fixture(run_dir.name, "drone-2", selected["waypointsEnuM"])
            write_json(run_dir / "flight-evidence-drone-2.json", {"missionProgress": events})
            matching_grant = next(
                event
                for event in events
                if event["event"] == "grant_issued" and event["targetEnuM"] == command
            )
            actuator_path = run_dir / "applier-neutral" / "actuator-output.jsonl"
            actuator_path.parent.mkdir(exist_ok=True)
            with actuator_path.open("a", encoding="utf-8") as stream:
                stream.write(
                    json.dumps(
                        {
                            "runId": run_dir.name,
                            "vehicleId": "drone-2",
                            "stage": "authorized-command",
                            "authority": "verified-core-grant",
                            "recordedAtUnixMs": matching_grant["atUnixMs"],
                            "sequence": 99,
                            "executionGrant": {
                                "runId": run_dir.name,
                                "vehicleId": "drone-2",
                                "grantSequence": matching_grant["grantSequence"],
                                "targetEnuMm": [10_000, 8_000, 10_000],
                            },
                        }
                    )
                    + "\n"
                )
            proof = json.loads((run_dir / "gz-flight-proof-report-drone-2.json").read_text())
            proof["timingReference"]["simulationTimeOriginSeconds"] = 2.8
            proof["measured"]["legs"].append({"index": 2, "reached": True, "simSeconds": 0.2})
            write_json(run_dir / "gz-flight-proof-report-drone-2.json", proof)
            with (run_dir / "gazebo-pose.jsonl").open("a", encoding="utf-8") as stream:
                for index in range(21):
                    stamp = 2.9 + index * 0.1
                    stream.write(
                        json.dumps(
                            {
                                "header": {
                                    "stamp": {"sec": int(stamp), "nsec": round((stamp % 1) * 1e9)}
                                },
                                "pose": [
                                    {"name": "fpv-a", "position": {"x": 10.0, "y": 8.0, "z": 10.0}}
                                ],
                            }
                        )
                        + "\n"
                    )
            config = load_swarm_config(run_dir)
            result = selected_vehicle_authorization_evidence(
                run_dir, plan, config["vehicles"], run_dir.name
            )
        self.assertEqual(result["status"], "failed", result)
        self.assertEqual(result["authorityArrivalSimulationTimeSeconds"], 1.6)
        self.assertEqual(result["arrivalSimulationTimeSeconds"], 3.0)
        self.assertEqual(result["targetRouteProgressFromPreviousWaypointM"], 0.0)

    def test_selected_vehicle_control_requires_matching_grant_and_peer_stability(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory) / "antoine-swarm-flight-positive-matrix"
            build_run(run_dir, "lanes")
            plan = json.loads((run_dir / "research-swarm-plan.json").read_text())
            selected = plan["vehicles"][1]
            previous = selected["waypointsEnuM"][-1]
            command = [10.0, 8.0, 10.0]
            selected["waypointsEnuM"].append(command)
            selected["finalSlotEnuM"] = command
            plan["selectedVehicleAuthorization"] = {
                "callsign": "drone-2",
                "previousWaypointEnuM": previous,
                "commandWaypointEnuM": command,
                "authorizationPath": "normal-run-local-authority-signed-grant",
            }
            write_json(run_dir / "research-swarm-plan.json", plan)
            events = mission_progress_fixture(run_dir.name, "drone-2", selected["waypointsEnuM"])
            write_json(run_dir / "flight-evidence-drone-2.json", {"missionProgress": events})
            grant = next(
                e for e in events if e["event"] == "grant_issued" and e["targetEnuM"] == command
            )
            actuator_path = run_dir / "applier-neutral" / "actuator-output.jsonl"
            with actuator_path.open("a", encoding="utf-8") as stream:
                stream.write(
                    json.dumps(
                        {
                            "runId": run_dir.name,
                            "vehicleId": "drone-2",
                            "stage": "authorized-command",
                            "authority": "verified-core-grant",
                            "recordedAtUnixMs": grant["atUnixMs"],
                            "sequence": 99,
                            "executionGrant": {
                                "runId": run_dir.name,
                                "vehicleId": "drone-2",
                                "grantSequence": grant["grantSequence"] + 1,
                                "targetEnuMm": [10000, 8000, 10000],
                            },
                        }
                    )
                    + "\n"
                )
            proof_path = run_dir / "gz-flight-proof-report-drone-2.json"
            proof = json.loads(proof_path.read_text())
            proof["timingReference"]["simulationTimeOriginSeconds"] = 2.8
            proof["measured"]["legs"].append({"index": 2, "reached": True, "simSeconds": 0.2})
            write_json(proof_path, proof)
            with (run_dir / "gazebo-pose.jsonl").open("a", encoding="utf-8") as stream:
                for index in range(21):
                    stamp = 2.9 + index * 0.1
                    stream.write(
                        json.dumps(
                            {
                                "header": {
                                    "stamp": {"sec": int(stamp), "nsec": round((stamp % 1) * 1e9)}
                                },
                                "pose": [
                                    {"name": "fpv-a", "position": {"x": 10.0, "y": 8.0, "z": 10.0}},
                                    {"name": "isr", "position": {"x": 0.0, "y": 8.0, "z": 10.0}},
                                ],
                            }
                        )
                        + "\n"
                    )
            config = load_swarm_config(run_dir)
            no_matching_grant = selected_vehicle_authorization_evidence(
                run_dir, plan, config["vehicles"], run_dir.name
            )
            self.assertEqual(no_matching_grant["status"], "inconclusive")
            self.assertEqual(
                no_matching_grant["reason"], "selected-command-actuator-grant-not-found"
            )

            actuator = json.loads(actuator_path.read_text().splitlines()[-1])
            actuator["executionGrant"]["grantSequence"] = grant["grantSequence"]
            with actuator_path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(actuator) + "\n")
            with (run_dir / "gazebo-pose.jsonl").open("a", encoding="utf-8") as stream:
                for index in range(21):
                    stamp = 2.9 + index * 0.1
                    stream.write(
                        json.dumps(
                            {
                                "header": {
                                    "stamp": {"sec": int(stamp), "nsec": round((stamp % 1) * 1e9)}
                                },
                                "pose": [
                                    {"name": "isr", "position": {"x": 2.0, "y": 8.0, "z": 10.0}}
                                ],
                            }
                        )
                        + "\n"
                    )
            peer_moved = selected_vehicle_authorization_evidence(
                run_dir, plan, config["vehicles"], run_dir.name
            )
            self.assertEqual(peer_moved["status"], "failed")
            self.assertGreater(peer_moved["peerWindowDisplacementM"], 1.0)

    def test_waypoint_snapshot_aligns_vehicles_with_different_arrival_times(self) -> None:
        vehicles = [
            {"callsign": "drone-1", "model": "isr"},
            {"callsign": "drone-2", "model": "fpv-a"},
        ]
        samples = {
            callsign: [(100.0 + index / 10, float(index), 0.0, 10.0) for index in range(11)]
            for callsign in ("drone-1", "drone-2")
        }
        reports = {
            "drone-1": {
                "timingReference": {
                    "source": "first-valid-gazebo-pose",
                    "simulationTimeOriginSeconds": 100.0,
                },
                "measured": {"legs": [{"reached": True, "simSeconds": 0.8}]},
            },
            "drone-2": {
                "timingReference": {
                    "source": "first-valid-gazebo-pose",
                    "simulationTimeOriginSeconds": 100.9,
                },
                "measured": {"legs": [{"reached": True, "simSeconds": 0.0}]},
            },
        }
        snapshot, timestamp = synchronized_waypoint_snapshot(vehicles, samples, reports)
        self.assertAlmostEqual(timestamp, 100.9)
        self.assertAlmostEqual(snapshot["drone-1"][-1][0], 100.9)
        self.assertAlmostEqual(snapshot["drone-2"][-1][0], 100.9)

    def test_missing_waypoint_clock_origin_keeps_behavior_inconclusive(self) -> None:
        vehicles = [
            {"callsign": "drone-1", "model": "isr"},
            {"callsign": "drone-2", "model": "fpv-a"},
        ]
        samples = {
            callsign: [(100.0 + index / 10, 0.0, float(index), 10.0) for index in range(11)]
            for callsign in ("drone-1", "drone-2")
        }
        reports = {
            callsign: {"measured": {"legs": [{"reached": True, "simSeconds": 1.0}]}}
            for callsign in ("drone-1", "drone-2")
        }
        snapshot, timestamp = synchronized_waypoint_snapshot(vehicles, samples, reports)
        self.assertEqual(snapshot, {})
        self.assertIsNone(timestamp)

    def test_malformed_waypoint_clock_origin_keeps_behavior_inconclusive(self) -> None:
        vehicles = [
            {"callsign": "drone-1", "model": "isr"},
            {"callsign": "drone-2", "model": "fpv-a"},
        ]
        samples = {
            callsign: [(100.0 + index / 10, 0.0, float(index), 10.0) for index in range(11)]
            for callsign in ("drone-1", "drone-2")
        }
        reports = {
            callsign: {
                "timingReference": {
                    "source": "first-valid-gazebo-pose",
                    "simulationTimeOriginSeconds": "100.0",
                },
                "measured": {"legs": [{"reached": True, "simSeconds": 1.0}]},
            }
            for callsign in ("drone-1", "drone-2")
        }
        snapshot, timestamp = synchronized_waypoint_snapshot(vehicles, samples, reports)
        self.assertEqual(snapshot, {})
        self.assertIsNone(timestamp)

    def test_perfect_vee_plan_passes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory) / "antoine-swarm-flight-test"
            build_run(run_dir, "vee")
            result = run_summary(run_dir)
        self.assertEqual(result["status"], "inconclusive")
        self.assertEqual(result["behaviorStatus"], "passed")
        self.assertEqual(result["probeStatus"], "not_requested")
        self.assertEqual(result["behavior"], "vee")
        self.assertTrue(result["behaviorEvaluation"]["gates"]["plannedFormationShapeMatched"])
        self.assertTrue(result["poseStreamQualityPassed"])
        self.assertTrue(result["baselineGates"]["gazeboAttitudeWithinEnvelope"])
        self.assertTrue(result["vehicles"]["drone-1"]["attitudeQuality"]["passed"])
        self.assertEqual(result["vehiclePairs"]["drone-1/drone-2"]["measurementStatus"], "measured")
        self.assertEqual(result["baselineStatus"], "inconclusive")
        self.assertIn("finalDisarmedStateAvailable", result["baselineInconclusiveReasons"])
        self.assertEqual(result["exitCode"], 2)
        self.assertAlmostEqual(result["formationEvaluationTimeSeconds"], 3.4)

    def test_scripted_slots_are_evaluated_before_post_neutralization_descent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory) / "antoine-swarm-flight-test"
            build_run(run_dir, "lanes")
            pose_path = run_dir / "gazebo-pose.jsonl"
            frames = [json.loads(line) for line in pose_path.read_text().splitlines()]
            for model in ("isr", "fpv-a"):
                frames.append(
                    {
                        "header": {"stamp": {"sec": 4, "nsec": 0}},
                        "pose": [
                            {
                                "name": model,
                                "position": {"x": 0.0, "y": 8.0, "z": 0.09}
                                if model == "isr"
                                else {"x": 8.0, "y": 8.0, "z": 0.09},
                                "orientation": {"x": 0.0, "y": 0.0, "z": 0.0, "w": 1.0},
                            }
                        ],
                    }
                )
            pose_path.write_text("".join(json.dumps(frame) + "\n" for frame in frames))
            result = run_summary(run_dir)
        self.assertEqual(result["behaviorEvaluation"]["status"], "passed")
        self.assertIsNotNone(result["formationEvaluationTimeSeconds"])
        self.assertTrue(result["allFlightsPassed"])
        self.assertEqual(result["status"], "inconclusive")
        self.assertEqual(result["exitCode"], 2)
        self.assertLess(
            result["vehicles"]["drone-1"]["postNeutralizationFinalPositionEnuM"][2],
            1.0,
        )

    def test_gazebo_inversion_fails_attitude_gate_independently_of_position(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory) / "antoine-swarm-flight-test"
            build_run(run_dir, "lanes")
            pose_path = run_dir / "gazebo-pose.jsonl"
            frames = [json.loads(line) for line in pose_path.read_text().splitlines()]
            for frame in frames:
                for pose in frame["pose"]:
                    if pose["name"] == "isr":
                        pose["orientation"] = {"x": 1.0, "y": 0.0, "z": 0.0, "w": 0.0}
                    else:
                        # Yaw is not tilt; heading changes must not fail this gate.
                        half_yaw = math.sqrt(0.5)
                        pose["orientation"] = {
                            "x": 0.0,
                            "y": 0.0,
                            "z": half_yaw,
                            "w": half_yaw,
                        }
            pose_path.write_text("".join(json.dumps(frame) + "\n" for frame in frames))
            result = run_summary(run_dir)
        attitude = result["vehicles"]["drone-1"]["attitudeQuality"]
        self.assertAlmostEqual(attitude["maximumTiltRadians"], 3.141593, places=5)
        self.assertFalse(attitude["passed"])
        self.assertFalse(result["baselineGates"]["gazeboAttitudeWithinEnvelope"])
        self.assertTrue(result["vehicles"]["drone-2"]["attitudeQuality"]["passed"])
        self.assertEqual(result["behaviorStatus"], "passed")
        self.assertEqual(result["baselineStatus"], "failed")
        self.assertEqual(result["baselineFailedReasons"], ["gazeboAttitudeWithinEnvelope"])
        self.assertNotIn("gazeboAttitudeWithinEnvelope", result["baselineInconclusiveReasons"])
        self.assertEqual(result["status"], "failed")

    def test_missing_gazebo_quaternion_makes_attitude_evidence_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory) / "antoine-swarm-flight-test"
            build_run(run_dir, "lanes")
            pose_path = run_dir / "gazebo-pose.jsonl"
            frames = [json.loads(line) for line in pose_path.read_text().splitlines()]
            for frame in frames:
                for pose in frame["pose"]:
                    if pose["name"] == "isr":
                        pose.pop("orientation")
            pose_path.write_text("".join(json.dumps(frame) + "\n" for frame in frames))
            result = run_summary(run_dir)
        attitude = result["vehicles"]["drone-1"]["attitudeQuality"]
        self.assertFalse(attitude["measured"])
        self.assertFalse(attitude["passed"])
        self.assertFalse(result["baselineGates"]["gazeboAttitudeWithinEnvelope"])
        self.assertEqual(result["baselineStatus"], "inconclusive")
        self.assertEqual(result["status"], "inconclusive")

    def test_missing_final_waypoint_time_keeps_behavior_inconclusive(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory) / "antoine-swarm-flight-test"
            build_run(run_dir, "lanes")
            report_path = run_dir / "gz-flight-proof-report-drone-2.json"
            report = json.loads(report_path.read_text())
            report["measured"]["legs"][-1].pop("simSeconds")
            write_json(report_path, report)
            result = run_summary(run_dir)
        self.assertEqual(result["status"], "inconclusive")
        self.assertIsNone(result["formationEvaluationTimeSeconds"])

    def test_pose_deviation_makes_the_run_inconclusive(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory) / "antoine-swarm-flight-test"
            build_run(run_dir, "vee", offset_second_vehicle=5.0)
            result = run_summary(run_dir)
        self.assertEqual(result["behaviorStatus"], "failed")
        self.assertEqual(result["status"], "failed")
        self.assertFalse(result["behaviorEvaluation"]["gates"]["allFinalSlotsWithinTolerance"])
        self.assertEqual(result["exitCode"], 2)

    def test_pose_stream_quality_rejects_stale_gapped_and_regressed_samples(self) -> None:
        stale = [(float(index), 0.0, 0.0, 10.0) for index in range(21)]
        self.assertFalse(measure_pose_stream(stale)["freshEnough"])
        regressed = [(float(index), 0.0, 0.0, 10.0) for index in range(21)]
        regressed[10] = (0.5, 0.0, 0.0, 10.0)
        self.assertFalse(measure_pose_stream(regressed)["orderedUniqueTimestamps"])

    def test_report_does_not_infer_disarm_from_actuator_neutralization(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory) / "antoine-swarm-flight-test"
            build_run(run_dir, "lanes")
            result = run_summary(run_dir)
        for vehicle in result["vehicles"].values():
            self.assertTrue(vehicle["actuatorEvidence"]["neutralizationPassed"])
            self.assertEqual(vehicle["actuatorEvidence"]["finalDisarmed"]["status"], "unavailable")
            self.assertIsNone(vehicle["actuatorEvidence"]["finalDisarmed"]["disarmed"])
        self.assertFalse(result["baselineGates"]["allVehiclesDisarmed"])
        self.assertTrue(result["baselineGates"]["allExecutionGrantsRevoked"])
        self.assertEqual(result["baselineStatus"], "inconclusive")
        self.assertEqual(result["status"], "inconclusive")
        self.assertEqual(result["exitCode"], 2)

    def test_execution_authority_requires_run_vehicle_and_explicit_boolean(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory) / "antoine-swarm-flight-test"
            build_run(run_dir, "lanes")
            evidence_path = run_dir / "flight-evidence-drone-2.json"
            evidence = json.loads(evidence_path.read_text())
            evidence["telemetry"][-1]["runId"] = "stale-run"
            write_json(evidence_path, evidence)
            result = run_summary(run_dir)
        self.assertEqual(
            result["vehicles"]["drone-2"]["finalExecutionAuthority"]["status"], "invalid"
        )
        self.assertFalse(result["baselineGates"]["allExecutionGrantsRevoked"])

    def test_mission_progress_is_bound_and_correlated_with_waypoint_evaluator(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory) / "antoine-swarm-flight-test"
            build_run(run_dir, "lanes")
            result = run_summary(run_dir)
        progress = result["vehicles"]["drone-1"]["missionProgress"]
        self.assertEqual(progress["status"], "measured")
        self.assertEqual(progress["waypointTransitions"], [0])
        self.assertEqual(len(progress["transitionBindings"]), 1)
        self.assertEqual(progress["transitionBindings"][0]["grantSequence"], 2)
        self.assertEqual(progress["transitionBindings"][0]["actuatorCommandCount"], 1)
        self.assertEqual(progress["missingEvaluatorTransitions"], [])
        safe_stop = result["vehicles"]["drone-1"]["safeStopEvidence"]
        self.assertEqual(safe_stop["status"], "measured")
        self.assertEqual(safe_stop["transitionStops"][0]["applierNeutralStopCommandCount"], 1)
        self.assertEqual(safe_stop["transitionStops"][0]["commandsDuringStopHold"], 0)
        self.assertEqual(
            result["vehicles"]["drone-1"]["missionProgressFailure"]["firstUnreachedWaypointIndex"],
            None,
        )

    def test_positional_arrival_does_not_hide_a_kinematic_dwell_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory) / "antoine-swarm-flight-test"
            build_run(run_dir, "lanes")
            qualify_fixture_baseline(run_dir)
            path = run_dir / "gz-flight-proof-report-drone-2.json"
            report = json.loads(path.read_text())
            report["localFlightPassed"] = True  # A summary flag cannot hide a failed leg.
            report["measured"]["legs"][1]["kinematicArrivalPassed"] = False
            write_json(path, report)
            result = run_summary(run_dir)
        failure = result["vehicles"]["drone-2"]["missionProgressFailure"]
        self.assertEqual(failure["status"], "failed")
        self.assertIsNone(failure["firstUnreachedWaypointIndex"])
        self.assertEqual(failure["firstKinematicallyUnqualifiedWaypointIndex"], 1)
        self.assertIn("continuous kinematic dwell did not pass", failure["explanation"])
        self.assertFalse(result["allFlightsPassed"])
        self.assertEqual(result["behaviorStatus"], "failed")
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["baselineStatus"], "inconclusive")
        self.assertFalse(result["baselineGates"]["allFlightsPassed"])

    def test_missing_progress_reports_last_transition_and_unreached_waypoint(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory) / "antoine-swarm-flight-test"
            build_run(run_dir, "lanes", progress_events=False)
            report_path = run_dir / "gz-flight-proof-report-drone-1.json"
            report = json.loads(report_path.read_text())
            report["measured"]["legs"][1]["reached"] = False
            report["measured"]["allLegsReached"] = False
            write_json(report_path, report)
            result = run_summary(run_dir)
        vehicle = result["vehicles"]["drone-1"]
        self.assertEqual(vehicle["missionProgress"]["status"], "unavailable")
        self.assertEqual(vehicle["missionProgressFailure"]["firstUnreachedWaypointIndex"], 1)

    def test_mission_progress_rejects_foreign_run_or_callsign(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory) / "antoine-swarm-flight-test"
            build_run(run_dir, "lanes")
            evidence = json.loads((run_dir / "flight-evidence-drone-1.json").read_text())
            evidence["missionProgress"][0]["runId"] = "other-run"
            write_json(run_dir / "flight-evidence-drone-1.json", evidence)
            result = run_summary(run_dir)
        self.assertEqual(result["vehicles"]["drone-1"]["missionProgress"]["status"], "invalid")

    def test_mission_progress_rejects_transition_without_matching_next_grant(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory) / "antoine-swarm-flight-test"
            build_run(run_dir, "lanes")
            evidence_path = run_dir / "flight-evidence-drone-1.json"
            evidence = json.loads(evidence_path.read_text())
            evidence["missionProgress"][3]["targetEnuM"] = [99.0, 99.0, 10.0]
            write_json(evidence_path, evidence)
            result = run_summary(run_dir)
        progress = result["vehicles"]["drone-1"]["missionProgress"]
        self.assertEqual(progress["status"], "invalid")
        self.assertIn("transition-grant-not-correlated", progress["invalidReasons"])
        self.assertFalse(result["allFlightsPassed"])

    def test_mission_progress_requires_actuator_command_for_transition_grant(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory) / "antoine-swarm-flight-test"
            build_run(run_dir, "lanes")
            actuator_path = run_dir / "applier-neutral" / "actuator-output.jsonl"
            actuator_path.write_text('{"runId":"other-run"}\n', encoding="utf-8")
            result = run_summary(run_dir)
        progress = result["vehicles"]["drone-1"]["missionProgress"]
        self.assertEqual(progress["status"], "invalid")
        self.assertIn("transition-actuator-not-correlated", progress["invalidReasons"])

    def test_mission_progress_requires_controller_admission_for_transition_vigie_sequence(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory) / "antoine-swarm-flight-test"
            build_run(run_dir, "lanes")
            (run_dir / "logs" / "antoine-controller-drone-1.log").write_text(
                "ANTOINE_GZ_VETO_RESULT callsign=drone-1 vigie_sequence=99 decision=execute\n",
                encoding="utf-8",
            )
            result = run_summary(run_dir)
        progress = result["vehicles"]["drone-1"]["missionProgress"]
        self.assertEqual(progress["status"], "invalid")
        self.assertIn("transition-controller-admission-not-confirmed", progress["invalidReasons"])
        failure = result["vehicles"]["drone-1"]["missionProgressFailure"]
        self.assertEqual(failure["status"], "failed")
        self.assertEqual(failure["firstUnrecordedTransitionWaypointIndex"], 0)
        self.assertFalse(result["allFlightsPassed"])

    def test_transition_safe_stop_requires_bounded_correlated_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory) / "antoine-swarm-flight-test"
            build_run(run_dir, "lanes")
            evidence_path = run_dir / "flight-evidence-drone-1.json"
            evidence = json.loads(evidence_path.read_text())
            evidence["stopEvents"].pop()
            write_json(evidence_path, evidence)
            result = run_summary(run_dir)
        safe_stop = result["vehicles"]["drone-1"]["safeStopEvidence"]
        self.assertEqual(safe_stop["status"], "invalid")
        self.assertIn("transition-stop-recovery-missing-or-ambiguous", safe_stop["invalidReasons"])
        self.assertFalse(result["baselineGates"]["transitionSafeStopsBoundAndRecovered"])

    def test_unconditional_actuator_neutralization_is_a_supported_safe_stop_decision(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory) / "antoine-swarm-flight-test"
            build_run(run_dir, "lanes")
            evidence_path = run_dir / "flight-evidence-drone-1.json"
            evidence = json.loads(evidence_path.read_text())
            evidence["stopEvents"][0]["controllerDecision"] = (
                "publish-unconditional-actuator-neutralization"
            )
            write_json(evidence_path, evidence)
            result = run_summary(run_dir)
        safe_stop = result["vehicles"]["drone-1"]["safeStopEvidence"]
        self.assertEqual(safe_stop["status"], "measured")
        self.assertEqual(
            result["vehicles"]["drone-1"]["safeStopEvidence"]["transitionStops"][0][
                "controllerDecision"
            ],
            "publish-unconditional-actuator-neutralization",
        )

    def test_transition_without_safe_stop_is_qualified_by_correlated_mission_progress(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory) / "antoine-swarm-flight-test"
            build_run(run_dir, "lanes")
            evidence_path = run_dir / "flight-evidence-drone-1.json"
            evidence = json.loads(evidence_path.read_text())
            evidence["stopEvents"] = []
            write_json(evidence_path, evidence)
            result = run_summary(run_dir)
        safe_stop = result["vehicles"]["drone-1"]["safeStopEvidence"]
        self.assertEqual(safe_stop["status"], "measured")
        self.assertTrue(safe_stop["noTransitionStopsObserved"])
        self.assertGreater(safe_stop["transitionCount"], 0)

    def test_transition_safe_stop_rejects_cross_run_stop_events(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory) / "antoine-swarm-flight-test"
            build_run(run_dir, "lanes")
            evidence_path = run_dir / "flight-evidence-drone-2.json"
            evidence = json.loads(evidence_path.read_text())
            evidence["stopEvents"][0]["runId"] = "other-run"
            write_json(evidence_path, evidence)
            result = run_summary(run_dir)
        self.assertEqual(result["vehicles"]["drone-2"]["safeStopEvidence"]["status"], "invalid")

    def test_transition_safe_stop_rejects_underreported_duration(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory) / "antoine-swarm-flight-test"
            build_run(run_dir, "lanes")
            evidence_path = run_dir / "flight-evidence-drone-1.json"
            evidence = json.loads(evidence_path.read_text())
            evidence["stopEvents"][1]["holdDurationMs"] = 1
            write_json(evidence_path, evidence)
            result = run_summary(run_dir)
        safe_stop = result["vehicles"]["drone-1"]["safeStopEvidence"]
        self.assertIn("transition-stop-recovery-unbounded-or-unbound", safe_stop["invalidReasons"])
        self.assertFalse(result["baselineGates"]["transitionSafeStopsBoundAndRecovered"])

    def test_transition_stop_requires_bound_monotonic_clock(self) -> None:
        for field, value in (
            ("clockSource", "wall-clock"),
            ("atMonotonicMs", 3_999),
            ("atMonotonicMs", True),
            ("atMonotonicMs", None),
            ("holdStartedAtMonotonicMs", 3_999),
            ("holdStartedAtMonotonicMs", True),
        ):
            with self.subTest(field=field, value=value), tempfile.TemporaryDirectory() as directory:
                run_dir = Path(directory) / "antoine-swarm-flight-test"
                build_run(run_dir, "lanes")
                evidence_path = run_dir / "flight-evidence-drone-1.json"
                evidence = json.loads(evidence_path.read_text())
                evidence["stopEvents"][1][field] = value
                write_json(evidence_path, evidence)
                result = run_summary(run_dir)
            self.assertEqual(result["vehicles"]["drone-1"]["safeStopEvidence"]["status"], "invalid")
            self.assertFalse(result["baselineGates"]["transitionSafeStopsBoundAndRecovered"])

    def test_transition_safe_stop_rejects_stale_or_reordered_applier_stop(self) -> None:
        for timestamp, sequence in ((1, 17), (10_100, 17), (9_001, 999)):
            with (
                self.subTest(timestamp=timestamp, sequence=sequence),
                tempfile.TemporaryDirectory() as directory,
            ):
                run_dir = Path(directory) / "antoine-swarm-flight-test"
                build_run(run_dir, "lanes")
                actuator_path = run_dir / "applier-neutral" / "actuator-output.jsonl"
                records = [json.loads(line) for line in actuator_path.read_text().splitlines()]
                for record in records:
                    if (
                        record.get("vehicleId") == "drone-1"
                        and record.get("stage") == "explicit-stop"
                    ):
                        record["recordedAtUnixMs"] = timestamp
                        record["sequence"] = sequence
                actuator_path.write_text("".join(json.dumps(row) + "\n" for row in records))
                result = run_summary(run_dir)
            safe_stop = result["vehicles"]["drone-1"]["safeStopEvidence"]
            self.assertEqual(safe_stop["status"], "invalid")
            self.assertFalse(result["baselineGates"]["transitionSafeStopsBoundAndRecovered"])

    def test_transition_safe_stop_rejects_actuator_command_during_hold(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory) / "antoine-swarm-flight-test"
            build_run(run_dir, "lanes")
            actuator_path = run_dir / "applier-neutral" / "actuator-output.jsonl"
            with actuator_path.open("a", encoding="utf-8") as stream:
                stream.write(
                    json.dumps(
                        {
                            "runId": run_dir.name,
                            "vehicleId": "drone-1",
                            "stage": "authorized-command",
                            "authority": "verified-core-grant",
                            "recordedAtUnixMs": 9_500,
                            "sequence": 99,
                            "executionGrant": {"grantSequence": 2},
                        }
                    )
                    + "\n"
                )
            result = run_summary(run_dir)
        safe_stop = result["vehicles"]["drone-1"]["safeStopEvidence"]
        self.assertEqual(safe_stop["status"], "invalid")
        self.assertIn(
            "transition-stop-applier-correlation-or-hold-integrity-failed",
            safe_stop["invalidReasons"],
        )

    def test_mission_progress_rejects_nonmonotonic_event_timestamps(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory) / "antoine-swarm-flight-test"
            build_run(run_dir, "lanes")
            evidence_path = run_dir / "flight-evidence-drone-1.json"
            evidence = json.loads(evidence_path.read_text())
            evidence["missionProgress"][2]["atUnixMs"] = 1000
            write_json(evidence_path, evidence)
            result = run_summary(run_dir)
        self.assertEqual(result["vehicles"]["drone-1"]["missionProgress"]["status"], "invalid")

    def test_mission_progress_rejects_duplicate_event_sequences(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory) / "antoine-swarm-flight-test"
            build_run(run_dir, "lanes")
            evidence_path = run_dir / "flight-evidence-drone-1.json"
            evidence = json.loads(evidence_path.read_text())
            evidence["missionProgress"][1]["eventSequence"] = evidence["missionProgress"][0][
                "eventSequence"
            ]
            write_json(evidence_path, evidence)
            result = run_summary(run_dir)
        progress = result["vehicles"]["drone-1"]["missionProgress"]
        self.assertEqual(progress["status"], "invalid")
        self.assertIn("identity-sequence-or-binding-invalid", progress["invalidReasons"])

    def test_missing_execution_authority_stays_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory) / "antoine-swarm-flight-test"
            build_run(run_dir, "lanes")
            evidence_path = run_dir / "flight-evidence-drone-2.json"
            evidence = json.loads(evidence_path.read_text())
            evidence.pop("telemetry")
            write_json(evidence_path, evidence)
            result = run_summary(run_dir)
        self.assertEqual(
            result["vehicles"]["drone-2"]["finalExecutionAuthority"]["status"], "unavailable"
        )
        self.assertFalse(result["baselineGates"]["allExecutionGrantsRevoked"])

    def test_state_rate_below_control_rate_makes_flight_inconclusive(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory) / "antoine-swarm-flight-test"
            build_run(run_dir, "lanes")
            evidence_path = run_dir / "flight-evidence-drone-2.json"
            evidence = json.loads(evidence_path.read_text())
            for sample in evidence["telemetry"]:
                sample["observedStateRateHz"] = 20.0
                sample["ticksPerStateSample"] = 1.25
            write_json(evidence_path, evidence)
            result = run_summary(run_dir)
        cadence = result["vehicles"]["drone-2"]["controllerStateCadence"]
        self.assertEqual(cadence["status"], "failed")
        self.assertAlmostEqual(cadence["controlToStateRateRatio"], 1.25)
        self.assertFalse(result["baselineGates"]["controllerStateCadenceQualified"])
        self.assertEqual(result["status"], "inconclusive")

    def test_missing_rate_telemetry_is_inconclusive(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory) / "antoine-swarm-flight-test"
            build_run(run_dir, "lanes")
            evidence_path = run_dir / "flight-evidence-drone-2.json"
            evidence = json.loads(evidence_path.read_text())
            evidence["telemetry"] = evidence["telemetry"][:1]
            write_json(evidence_path, evidence)
            result = run_summary(run_dir)
        self.assertEqual(
            result["vehicles"]["drone-2"]["controllerStateCadence"]["status"], "unavailable"
        )
        self.assertFalse(result["baselineGates"]["controllerStateCadenceQualified"])
        self.assertEqual(result["status"], "inconclusive")

    def test_lanes_without_plan_artifact_remains_readable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory) / "antoine-swarm-flight-test"
            build_run(run_dir, "lanes", write_plan=False)
            result = run_summary(run_dir)
        self.assertEqual(result["swarmPlanSource"], "inferred-lanes-from-config")
        self.assertEqual(result["status"], "inconclusive")

    def test_independent_artifact_gate_does_not_require_plan_or_config(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory) / "antoine-swarm-flight-test"
            build_run(run_dir, "lanes")
            (run_dir / "research-swarm-plan.json").unlink()
            (run_dir / "research-swarm-config.json").unlink()
            result = run_summary(run_dir)
        self.assertTrue(result["baselineGates"]["independentPoseAndActuatorArtifactsPresent"])

    def test_complete_baseline_and_behavior_are_required_for_aggregate_pass(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory) / "antoine-swarm-flight-test"
            build_run(run_dir, "vee")
            qualify_fixture_baseline(run_dir)
            result = run_summary(run_dir)
        self.assertEqual(result["behaviorStatus"], "passed")
        self.assertEqual(result["probeStatus"], "not_requested")
        self.assertEqual(result["baselineStatus"], "qualified_baseline")
        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["exitCode"], 0)

    def test_probe_pass_does_not_hide_inconclusive_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory) / "antoine-swarm-flight-probe-test"
            build_run(run_dir, "lanes")
            config_path = run_dir / "research-swarm-config.json"
            config = json.loads(config_path.read_text(encoding="utf-8"))
            config["fault"] = "cross-vehicle-authorization"
            write_json(config_path, config)
            write_json(
                run_dir / "research-cross-vehicle-authorization.json",
                {
                    "runId": run_dir.name,
                    "status": "passed",
                    "publishedMessages": 1,
                    "mutationSupport": {"status": "supported"},
                    "controllerDecision": {"status": "rejected"},
                    "effectComparison": {
                        "runId": run_dir.name,
                        "status": "measured",
                        "poseEffectToleranceM": 1.0,
                        "vehicles": {
                            callsign: {
                                "model": model,
                                "poseDisplacementM": 0.1,
                                "poseElapsedSimulationSeconds": 1.0,
                                "actuatorIdentityValid": True,
                                "actuatorSequenceBefore": 1,
                                "actuatorSequenceAfter": 2,
                            }
                            for callsign, model in CALLSIGNS
                        },
                    },
                    "effectSnapshotBefore": {},
                    "effectSnapshotAtDecision": {},
                    "effectSnapshotAfterObservation": {},
                    "injectionAtUnixMs": 2000,
                },
            )
            authorization = json.loads(
                (run_dir / "research-cross-vehicle-authorization.json").read_text()
            )
            plan = json.loads((run_dir / "research-swarm-plan.json").read_text())
            stamps = [1000, 2000, 3000]
            sim_times = [1.0, 2.0, 3.0]
            for key, stamp, sim_time, y in zip(
                (
                    "effectSnapshotBefore",
                    "effectSnapshotAtDecision",
                    "effectSnapshotAfterObservation",
                ),
                stamps,
                sim_times,
                (1.0, 1.1, 1.2),
                strict=True,
            ):
                authorization[key] = {
                    "observedAtUnixMs": stamp,
                    "poseSimulationTimeSeconds": sim_time,
                    "poseByVehicle": {
                        vehicle["model"]: {
                            "model": vehicle["model"],
                            "positionEnuM": [0.0, y, 10.0],
                            "simulationTimeSeconds": sim_time,
                        }
                        for vehicle in plan["vehicles"]
                    },
                    "actuatorByVehicle": {
                        callsign: {
                            "runId": run_dir.name,
                            "vehicleId": callsign,
                            "sequence": index + 1,
                            "stage": "authorized-command",
                            "executionGrantRunId": run_dir.name,
                            "executionGrantVehicleId": callsign,
                        }
                        for index, (callsign, _model) in enumerate(CALLSIGNS)
                    },
                }
            write_json(run_dir / "research-cross-vehicle-authorization.json", authorization)
            result = run_summary(run_dir)
        self.assertEqual(result["probeStatus"], "passed")
        self.assertEqual(result["behaviorStatus"], "passed")
        self.assertEqual(result["baselineStatus"], "inconclusive")
        self.assertEqual(result["status"], "inconclusive")
        self.assertEqual(result["exitCode"], 2)

    def test_cross_vehicle_effect_windows_recompute_bounds_and_identity(self) -> None:
        vehicles = [{"callsign": callsign, "model": model} for callsign, model in CALLSIGNS]
        report = {"injectionAtUnixMs": 2000, "effectComparison": {"poseEffectToleranceM": 1.0}}
        for key, stamp, sim_time, y in zip(
            ("effectSnapshotBefore", "effectSnapshotAtDecision", "effectSnapshotAfterObservation"),
            (1000, 2000, 3000),
            (1.0, 2.0, 3.0),
            (0.0, 0.1, 0.2),
            strict=True,
        ):
            report[key] = {
                "observedAtUnixMs": stamp,
                "poseSimulationTimeSeconds": sim_time,
                "poseByVehicle": {
                    model: {
                        "model": model,
                        "positionEnuM": [0.0, y, 10.0],
                        "simulationTimeSeconds": sim_time,
                    }
                    for _callsign, model in CALLSIGNS
                },
                "actuatorByVehicle": {
                    callsign: {
                        "runId": "run-1",
                        "vehicleId": callsign,
                        "sequence": stamp,
                        "stage": "authorized-command",
                        "executionGrantRunId": "run-1",
                        "executionGrantVehicleId": callsign,
                    }
                    for callsign, _model in CALLSIGNS
                },
            }
        report["effectComparison"]["poseEffectToleranceM"] = 1.0
        self.assertTrue(cross_vehicle_effect_windows_valid(report, vehicles, "run-1"))
        report["effectSnapshotAfterObservation"]["actuatorByVehicle"]["drone-2"]["runId"] = (
            "other-run"
        )
        self.assertFalse(cross_vehicle_effect_windows_valid(report, vehicles, "run-1"))

    def test_cross_vehicle_windows_fail_closed_for_missing_stale_reversed_and_boundary_data(
        self,
    ) -> None:
        vehicles = [{"callsign": callsign, "model": model} for callsign, model in CALLSIGNS]

        def fixture() -> dict:
            report = {"injectionAtUnixMs": 2000, "effectComparison": {"poseEffectToleranceM": 1.0}}
            for key, stamp, sim_time, y in zip(
                (
                    "effectSnapshotBefore",
                    "effectSnapshotAtDecision",
                    "effectSnapshotAfterObservation",
                ),
                (1000, 2000, 3000),
                (1.0, 2.0, 3.0),
                (0.0, 0.1, 0.2),
                strict=True,
            ):
                report[key] = {
                    "observedAtUnixMs": stamp,
                    "poseSimulationTimeSeconds": sim_time,
                    "poseByVehicle": {
                        model: {
                            "model": model,
                            "positionEnuM": [0.0, y, 10.0],
                            "simulationTimeSeconds": sim_time,
                        }
                        for _callsign, model in CALLSIGNS
                    },
                    "actuatorByVehicle": {
                        callsign: {"runId": "run-1", "vehicleId": callsign, "sequence": stamp}
                        for callsign, _model in CALLSIGNS
                    },
                }
            return report

        cases = []
        missing = fixture()
        del missing["effectSnapshotAtDecision"]["poseByVehicle"]["fpv-a"]
        cases.append(("missing vehicle at decision", missing))
        stale = fixture()
        stale["effectSnapshotAfterObservation"]["observedAtUnixMs"] = 4001
        cases.append(("observation exceeds two seconds", stale))
        reversed_time = fixture()
        reversed_time["effectSnapshotAtDecision"]["poseSimulationTimeSeconds"] = 0.9
        cases.append(("simulation time regresses", reversed_time))
        boundary = fixture()
        boundary["effectSnapshotAfterObservation"]["poseByVehicle"]["fpv-a"]["positionEnuM"] = [
            1.2,
            0.2,
            10.0,
        ]
        cases.append(("effect exceeds one metre", boundary))
        decision_boundary = fixture()
        decision_boundary["effectSnapshotAtDecision"]["poseByVehicle"]["fpv-a"]["positionEnuM"] = [
            1.01,
            0.1,
            10.0,
        ]
        cases.append(("decision pose exceeds one metre", decision_boundary))
        decision_stale = fixture()
        decision_stale["effectSnapshotAtDecision"]["poseByVehicle"]["fpv-a"][
            "simulationTimeSeconds"
        ] = 3.1
        cases.append(("decision pose is newer than after window", decision_stale))
        decision_sequence = fixture()
        decision_sequence["effectSnapshotAtDecision"]["actuatorByVehicle"]["drone-2"][
            "sequence"
        ] = 9999
        cases.append(("actuator sequence regresses after decision", decision_sequence))
        for label, report in cases:
            with self.subTest(label=label):
                self.assertFalse(cross_vehicle_effect_windows_valid(report, vehicles, "run-1"))

    def test_failed_probe_makes_aggregate_status_failed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory) / "antoine-swarm-flight-probe-failed-test"
            build_run(run_dir, "lanes")
            config_path = run_dir / "research-swarm-config.json"
            config = json.loads(config_path.read_text(encoding="utf-8"))
            config["fault"] = "cross-vehicle-authorization"
            write_json(config_path, config)
            write_json(
                run_dir / "research-cross-vehicle-authorization.json",
                {"runId": run_dir.name, "status": "failed", "publishedMessages": 1},
            )
            result = run_summary(run_dir)
        self.assertEqual(result["probeStatus"], "failed")
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["exitCode"], 2)

    def test_command_rate_probe_status_is_bound_into_aggregate_report(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory) / "antoine-swarm-flight-rate-probe-test"
            build_run(run_dir, "lanes")
            config_path = run_dir / "research-swarm-config.json"
            config = json.loads(config_path.read_text(encoding="utf-8"))
            config["fault"] = "command-rate"
            write_json(config_path, config)
            probe = {
                "contract": "damn-vulnerable-drone.antoine-setpoint-rate-probe",
                "version": "1.0.0",
                "runId": run_dir.name,
                "status": "inconclusive",
                "publishedMessages": 40,
                "rejectedMessages": 38,
            }
            write_json(run_dir / "research-swarm-rate-disruption.json", probe)
            result = run_summary(run_dir)
        self.assertTrue(result["rateProbeRequested"])
        self.assertEqual(result["rateProbeObservation"], probe)
        self.assertEqual(result["probeStatus"], "inconclusive")
        self.assertEqual(result["status"], "inconclusive")
        self.assertTrue(
            result["evidenceArtifacts"]["research-swarm-rate-disruption.json"]["available"]
        )


if __name__ == "__main__":
    unittest.main()
