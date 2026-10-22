#!/usr/bin/env python3
"""Probe one run-local cross-vehicle setpoint binding using a live payload."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import subprocess
import tempfile
import time
from pathlib import Path
from urllib.parse import urlsplit

from dvd_research.run_files import read_run_tail_text, read_run_text, resolve_run_file
from dvd_research.run_nats import validate_run_nats_endpoint
from dvd_research.strict_json import loads as strict_json_loads

SUPPORTED_MUTATIONS = {"wrong-callsign", "wrong-task", "wrong-target", "stale-run"}
MAX_PROBE_MESSAGES = 1
MAX_POSE_BYTES = 512_000
MAX_ACTUATOR_BYTES = 512_000
POSE_EFFECT_TOLERANCE_M = 1.0
ACTUATOR_OUTPUT_EFFECT_TOLERANCE_L2 = 0.0
PROBE_SEQUENCE_GAP = 100
DECISION_TIMEOUT_SECONDS = 5.0


def finite_number(value: object) -> bool:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def validate_server(server: str) -> None:
    parsed = urlsplit(server)
    if (
        parsed.scheme != "nats"
        or parsed.hostname != "127.0.0.1"
        or parsed.port is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("refusing any NATS endpoint except explicit IPv4 loopback")


def validate_vehicle_callsigns(source: str, target: str) -> None:
    for label, callsign in (("source", source), ("target", target)):
        if (
            not isinstance(callsign, str)
            or not callsign
            or any(
                ord(character) < 33 or ord(character) == 127 or character in ".*>"
                for character in callsign
            )
        ):
            raise ValueError(f"{label} callsign must be a single NATS subject token")
    if source == target:
        raise ValueError("probe requires distinct vehicles")


def read_one_json(output: str) -> dict:
    for line in output.splitlines():
        try:
            value = strict_json_loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    raise ValueError("NATS raw subscription did not return a JSON object")


def mutate_setpoint(
    payload: dict, mutation: str, source: str, target: str, expected_run_id: str
) -> tuple[dict | None, dict]:
    """Apply one binding mutation; bump sequence only to keep it a fresh command."""
    if mutation not in SUPPORTED_MUTATIONS:
        raise ValueError(f"unsupported mutation {mutation}")
    if (
        payload.get("contract") != "furia.gzsitl.mission-setpoint"
        or payload.get("version") != "2.2.0"
        or payload.get("vehicleId") != target
        or not isinstance(payload.get("runId"), str)
        or not payload.get("runId")
        or payload.get("runId") != expected_run_id
        or not isinstance(payload.get("sequence"), int)
        or isinstance(payload.get("sequence"), bool)
    ):
        raise ValueError("captured setpoint does not match the expected live target contract")
    mutated = dict(payload)
    if mutation == "stale-run":
        mutated["runId"] = f"stale-{payload['runId']}"
        field, before, after = "runId", payload["runId"], mutated["runId"]
    elif mutation == "wrong-callsign":
        mutated["vehicleId"] = source
        field, before, after = "vehicleId", target, source
    elif mutation == "wrong-task":
        old_task = payload.get("taskId")
        if not isinstance(old_task, str) or not old_task:
            raise ValueError("captured setpoint has no task binding")
        new_task = f"dvd-cross-vehicle-unauthorized-{target}"
        if new_task == old_task:
            new_task += "-mutated"
        mutated["taskId"] = new_task
        old_correlation = payload.get("vigieAdmissionCorrelationId")
        admission_sequence = payload.get("vigieAdmissionSequence")
        if (
            not isinstance(old_correlation, str)
            or type(admission_sequence) is not int
            or not old_correlation.endswith(f"-{old_task}-{admission_sequence}")
        ):
            raise ValueError("captured setpoint has no correlated Vigie admission")
        prefix = old_correlation[: -len(old_task) - len(str(admission_sequence)) - 1]
        mutated["vigieAdmissionCorrelationId"] = f"{prefix}{new_task}-{admission_sequence}"
        field, before, after = "taskId", old_task, new_task
    else:
        old_position = payload.get("position_enu_m")
        if (
            not isinstance(old_position, list)
            or len(old_position) != 3
            or any(
                not isinstance(value, (int, float)) or isinstance(value, bool)
                for value in old_position
            )
            or any(not math.isfinite(value) for value in old_position)
        ):
            raise ValueError("captured setpoint has no finite ENU position target")
        new_position = list(old_position)
        new_position[0] += 2.0
        mutated["position_enu_m"] = new_position
        field, before, after = "position_enu_m", old_position, new_position
    original_sequence = payload["sequence"]
    # Keep the injected sequence distinct from a concurrent authority refresh.
    mutated["sequence"] = original_sequence + PROBE_SEQUENCE_GAP
    return mutated, {
        "status": "supported",
        "field": field,
        "before": before,
        "after": after,
        "additionalFields": ["vigieAdmissionCorrelationId"] if mutation == "wrong-task" else [],
        "sequenceAdvancedForFreshness": True,
        "sequenceGap": PROBE_SEQUENCE_GAP,
        "raceHeadroom": "separates the injected sequence from concurrent authority refreshes",
        "originalSequence": original_sequence,
        "probeSequence": mutated["sequence"],
    }


def target_plan(run_dir: Path, callsign: str) -> tuple[str, list[float]]:
    config_text = read_run_text(run_dir, Path("research-swarm-config.json"))
    if config_text is None:
        raise ValueError("run-owned swarm config is unavailable")
    config = strict_json_loads(config_text)
    if not isinstance(config, dict) or not isinstance(config.get("vehicles"), list):
        raise ValueError("swarm config must contain a vehicle roster")
    matches = [
        item
        for item in config["vehicles"]
        if isinstance(item, dict) and item.get("callsign") == callsign
    ]
    if len(matches) != 1:
        raise ValueError("run plan does not have exactly one target callsign")
    vehicle = matches[0]
    waypoints = vehicle.get("waypointsEnuM")
    if not isinstance(waypoints, list) or not waypoints:
        raise ValueError("target vehicle has no planned final waypoint")
    model = vehicle.get("model")
    target = waypoints[-1]
    if (
        not isinstance(model, str)
        or not isinstance(target, list)
        or len(target) != 3
        or any(not finite_number(value) for value in target)
    ):
        raise ValueError("target final waypoint is malformed")
    return model, [float(value) for value in target]


def vehicle_models_from_config(config: object) -> dict[str, str]:
    if not isinstance(config, dict) or not isinstance(config.get("vehicles"), list):
        raise ValueError("swarm config must contain a vehicle roster")
    vehicles = config["vehicles"]
    if len(vehicles) < 2:
        raise ValueError("swarm config must identify at least two vehicles")
    models = {}
    for item in vehicles:
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("callsign"), str)
            or not item["callsign"]
            or not isinstance(item.get("model"), str)
            or not item["model"]
        ):
            raise ValueError("swarm config contains a malformed vehicle identity")
        callsign, model = item["callsign"], item["model"]
        if callsign in models or model in models.values():
            raise ValueError("swarm config callsigns and models must be unique")
        models[callsign] = model
    return models


def validate_probe_roster(models: dict[str, str], source: str, target: str) -> None:
    if source not in models or target not in models:
        raise ValueError("source and target callsigns must belong to the run roster")


def validate_run_evidence_paths(run_dir: Path, target: str) -> None:
    """Reject symlinked run evidence paths before using them as measurements."""
    if run_dir.is_symlink():
        raise ValueError("selected run directory is a symlink")
    root = run_dir.resolve(strict=True)
    if not root.is_dir():
        raise ValueError("selected run path is not a directory")
    paths = (
        Path("research-swarm-config.json"),
        Path("gazebo-pose.jsonl"),
        Path("applier-neutral/actuator-output.jsonl"),
        Path("logs") / f"antoine-controller-{target}.log",
    )
    for relative_path in paths:
        try:
            resolve_run_file(root, relative_path)
        except (OSError, RuntimeError, ValueError) as error:
            raise ValueError(
                f"run evidence path is unsafe in the selected run: {relative_path}"
            ) from error


def pose_simulation_time(stamp: object) -> float | None:
    """Parse a ROS timestamp without accepting booleans or fractional fields."""
    if not isinstance(stamp, dict):
        return None

    def integer_field(value: object) -> int | None:
        if isinstance(value, bool):
            return None
        if isinstance(value, int):
            return value
        if isinstance(value, float) and math.isfinite(value) and value.is_integer():
            return int(value)
        if isinstance(value, str):
            try:
                return int(value)
            except ValueError:
                return None
        return None

    seconds = integer_field(stamp.get("sec"))
    nanoseconds = integer_field(stamp.get("nsec"))
    if (
        seconds is None
        or seconds < 0
        or nanoseconds is None
        or not 0 <= nanoseconds < 1_000_000_000
    ):
        return None
    try:
        return seconds + nanoseconds / 1_000_000_000
    except OverflowError:
        return None


def vehicle_effect_snapshot(run_dir: Path, models: dict[str, str]) -> dict:
    """Capture bounded independent pose and applier state for every vehicle."""
    result = {
        "observedAtUnixMs": time.time_ns() // 1_000_000,
        "poseSimulationTimeSeconds": None,
        "poseByVehicle": {},
        "actuatorByVehicle": {},
        "missing": [],
    }
    try:
        pose_text = read_run_tail_text(run_dir, Path("gazebo-pose.jsonl"), MAX_POSE_BYTES)
    except OSError:
        pose_text = None
    lines = (pose_text or "").splitlines()
    wanted_models = set(models.values())
    for line in reversed(lines):
        try:
            frame = strict_json_loads(line)
            stamp = frame["header"]["stamp"]
            sim_time = pose_simulation_time(stamp)
        except (json.JSONDecodeError, KeyError, TypeError, ValueError, AttributeError):
            continue
        if sim_time is None or not math.isfinite(sim_time):
            continue
        poses = frame.get("pose")
        if not isinstance(poses, list):
            poses = [frame]
        for pose in poses:
            if not isinstance(pose, dict) or pose.get("name") not in wanted_models:
                continue
            model = pose["name"]
            if any(item.get("model") == model for item in result["poseByVehicle"].values()):
                continue
            try:
                position = [float(pose["position"][axis]) for axis in ("x", "y", "z")]
            except (KeyError, TypeError, ValueError, OverflowError):
                continue
            if not all(math.isfinite(value) for value in position):
                continue
            result["poseByVehicle"][model] = {
                "model": model,
                "simulationTimeSeconds": sim_time,
                "positionEnuM": position,
            }
        if result["poseSimulationTimeSeconds"] is None:
            result["poseSimulationTimeSeconds"] = sim_time
        if len(result["poseByVehicle"]) == len(models):
            break
    try:
        actuator_text = read_run_tail_text(
            run_dir, Path("applier-neutral/actuator-output.jsonl"), MAX_ACTUATOR_BYTES
        )
    except OSError:
        actuator_text = None
    actuator_lines = (actuator_text or "").splitlines()
    wanted_callsigns = set(models)
    for line in reversed(actuator_lines):
        try:
            row = strict_json_loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(row, dict):
            continue
        callsign = row.get("vehicleId")
        if callsign not in wanted_callsigns or callsign in result["actuatorByVehicle"]:
            continue
        grant = row.get("executionGrant")
        if not isinstance(grant, dict):
            continue
        result["actuatorByVehicle"][callsign] = {
            "runId": row.get("runId"),
            "vehicleId": callsign,
            "sequence": row.get("sequence"),
            "stage": row.get("stage"),
            "recordedAtUnixMs": row.get("recordedAtUnixMs"),
            "executionGrantRunId": grant.get("runId"),
            "executionGrantVehicleId": grant.get("vehicleId"),
            "authority": row.get("authority"),
            "actuatorKind": row.get("actuatorKind"),
            "neutral": row.get("neutral"),
            "values": row.get("values"),
        }
        if len(result["actuatorByVehicle"]) == len(models):
            break
    for callsign, model in models.items():
        if model not in {pose.get("model") for pose in result["poseByVehicle"].values()}:
            result["missing"].append(f"pose:{callsign}")
        if callsign not in result["actuatorByVehicle"]:
            result["missing"].append(f"actuator:{callsign}")
    return result


def actuator_numeric_vector(values: object) -> list[float] | None:
    if not isinstance(values, dict):
        return None
    vector: list[float] = []
    for key in sorted(values):
        value = values[key]
        candidates = value if isinstance(value, list) else [value]
        if any(not finite_number(item) for item in candidates):
            return None
        vector.extend(float(item) for item in candidates)
    return vector or None


def compare_effect_snapshots(
    before: dict, after: dict, models: dict[str, str], run_id: str
) -> dict:
    """Measure bounded per-vehicle pose and actuator changes across a probe."""
    vehicles = {}
    missing = list(before.get("missing", [])) + list(after.get("missing", []))
    for callsign, model in models.items():
        first = before.get("poseByVehicle", {}).get(model)
        last = after.get("poseByVehicle", {}).get(model)
        first_actuator = before.get("actuatorByVehicle", {}).get(callsign)
        last_actuator = after.get("actuatorByVehicle", {}).get(callsign)
        if not first or not last or not first_actuator or not last_actuator:
            missing.append(callsign)
            continue
        elapsed = last["simulationTimeSeconds"] - first["simulationTimeSeconds"]
        displacement = math.dist(first["positionEnuM"], last["positionEnuM"])
        first_sequence = first_actuator.get("sequence")
        last_sequence = last_actuator.get("sequence")
        identity_valid = all(
            actuator.get(key) == expected
            for actuator in (first_actuator, last_actuator)
            for key, expected in (
                ("runId", run_id),
                ("vehicleId", callsign),
                ("executionGrantRunId", run_id),
                ("executionGrantVehicleId", callsign),
            )
        )
        first_vector = actuator_numeric_vector(first_actuator.get("values"))
        last_vector = actuator_numeric_vector(last_actuator.get("values"))
        actuator_delta = (
            math.dist(first_vector, last_vector)
            if first_vector is not None
            and last_vector is not None
            and len(first_vector) == len(last_vector)
            else None
        )
        vehicles[callsign] = {
            "model": model,
            "poseElapsedSimulationSeconds": round(elapsed, 6),
            "poseDisplacementM": round(displacement, 6),
            "actuatorSequenceBefore": first_sequence,
            "actuatorSequenceAfter": last_sequence,
            "actuatorSequenceAdvanced": (
                type(first_sequence) is int
                and type(last_sequence) is int
                and last_sequence > first_sequence
            ),
            "actuatorIdentityValid": identity_valid,
            "actuatorValuesBefore": first_actuator.get("values"),
            "actuatorValuesAfter": last_actuator.get("values"),
            "actuatorValuesDeltaL2": actuator_delta,
            "actuatorAuthorityBefore": first_actuator.get("authority"),
            "actuatorAuthorityAfter": last_actuator.get("authority"),
        }
    complete = not missing and len(vehicles) == len(models)
    actuator_measurements_complete = complete and all(
        item["actuatorValuesDeltaL2"] is not None for item in vehicles.values()
    )
    actuator_outputs_stable = actuator_measurements_complete and all(
        item["actuatorValuesDeltaL2"] <= ACTUATOR_OUTPUT_EFFECT_TOLERANCE_L2
        for item in vehicles.values()
    )
    within_tolerance = (
        complete
        and all(
            item["poseElapsedSimulationSeconds"] > 0
            and item["poseDisplacementM"] <= POSE_EFFECT_TOLERANCE_M
            and item["actuatorSequenceAdvanced"]
            and item["actuatorIdentityValid"]
            for item in vehicles.values()
        )
        and actuator_outputs_stable
    )
    if not actuator_measurements_complete:
        status = "inconclusive"
        actuator_effect_status = "measurement-incomplete"
    elif not actuator_outputs_stable:
        status = "inconclusive"
        actuator_effect_status = "output-drift-exceeds-bound"
    elif within_tolerance:
        status = "measured"
        actuator_effect_status = "within-bound"
    else:
        status = "failed"
        actuator_effect_status = "within-bound"
    return {
        "status": status,
        "runId": run_id,
        "poseEffectToleranceM": POSE_EFFECT_TOLERANCE_M,
        "actuatorOutputEffectToleranceL2": ACTUATOR_OUTPUT_EFFECT_TOLERANCE_L2,
        "actuatorEffectStatus": actuator_effect_status,
        "missing": missing,
        "vehicles": vehicles,
    }


def pose_window_at_target(
    pose_path: Path,
    model: str,
    target: list[float],
    *,
    radius_m: float = 1.5,
    max_speed_mps: float = 0.6,
    dwell_seconds: float = 1.0,
    max_gap_seconds: float = 0.1,
) -> dict | None:
    """Return an independently measured waypoint dwell using flight thresholds."""
    try:
        pose_text = read_run_tail_text(pose_path.parent, Path(pose_path.name), MAX_POSE_BYTES)
    except OSError:
        return None
    if pose_text is None:
        return None
    lines = pose_text.splitlines()
    samples = []
    for line in lines:
        try:
            frame = strict_json_loads(line)
            stamp = frame["header"]["stamp"]
            timestamp = pose_simulation_time(stamp)
        except (json.JSONDecodeError, KeyError, TypeError, ValueError, AttributeError):
            continue
        if timestamp is None or not math.isfinite(timestamp):
            continue
        poses = frame.get("pose")
        if not isinstance(poses, list):
            poses = [frame]
        for pose in poses:
            if not isinstance(pose, dict) or pose.get("name") != model:
                continue
            position = pose.get("position")
            try:
                xyz = [float(position[axis]) for axis in ("x", "y", "z")]
            except (KeyError, TypeError, ValueError, OverflowError):
                continue
            if not all(math.isfinite(value) for value in xyz):
                continue
            distance = math.dist(xyz, target)
            samples.append((timestamp, distance, xyz))
    samples = samples[-100:]
    if len(samples) < 2:
        return None
    window = []
    speeds = []
    for sample in reversed(samples):
        if sample[1] > radius_m:
            break
        if window:
            elapsed = window[-1][0] - sample[0]
            if not 0 < elapsed <= max_gap_seconds:
                break
            speed = math.dist(window[-1][2], sample[2]) / elapsed
            if speed > max_speed_mps:
                break
            speeds.append(speed)
        window.append(sample)
    if len(window) < 2 or window[0][0] - window[-1][0] < dwell_seconds:
        return None
    return {
        "model": model,
        "targetEnuM": target,
        "radiusM": radius_m,
        "maximumSpeedMps": max_speed_mps,
        "dwellSeconds": round(window[0][0] - window[-1][0], 3),
        "firstSimulationTimeSeconds": round(window[-1][0], 6),
        "lastSimulationTimeSeconds": round(window[0][0], 6),
        "sampleCount": len(window),
        "finalPositionErrorM": round(window[0][1], 4),
        "maximumObservedSpeedMps": round(max(speeds, default=0.0), 4),
    }


def controller_decision(
    mutation: str,
    new_log_lines: list[str],
    sequence: int,
    target: str,
    mutation_value: str,
    payload_sha256: str,
) -> dict:
    received = any(
        f"ANTOINE_GZ_SETPOINT_RECEIVED callsign={target} sequence={sequence} " in line
        and f"payload_sha256={payload_sha256}" in line
        for line in new_log_lines
    )
    expected_rejected_vehicle = mutation_value if mutation == "wrong-callsign" else target
    rejected = next(
        (
            line
            for line in new_log_lines
            if f"ANTOINE_GZ_SETPOINT_REJECTED callsign={target} " in line
            and f"sequence={sequence}" in line.split()
            and f"vehicle_id={expected_rejected_vehicle}" in line.split()
            and (
                f"payload_sha256={payload_sha256}" in line.split()
                or not any(token.startswith("payload_sha256=") for token in line.split())
            )
        ),
        None,
    )
    if mutation == "wrong-callsign":
        return {
            "status": "rejected" if rejected and not received else "inconclusive",
            "parserRejected": rejected is not None,
            "candidateSetpointReceived": received,
            "reason": rejected.split("reason=", 1)[1].split()[0] if rejected else None,
            "rejectionEvidence": rejected,
            "correlation": "payload-sha256"
            if rejected and f"payload_sha256={payload_sha256}" in rejected
            else "unique-sequence-and-vehicle-id"
            if rejected
            else None,
            "mutatedVehicleId": mutation_value,
        }
    if mutation in {"wrong-task", "wrong-target", "stale-run"}:
        return {
            "status": "rejected-before-active-setpoint"
            if rejected and not received
            else "inconclusive",
            "parserRejected": rejected is not None,
            "candidateSetpointReceived": received,
            "rejectionEvidence": rejected,
            "activeSetpointSequenceNotAdvanced": rejected is not None and not received,
        }
    return {"status": "unsupported", "reason": f"unsupported mutation {mutation}"}


def publish_json(server: str, subject: str, payload: dict) -> None:
    subprocess.run(
        [
            "nats",
            "--no-context",
            "--server",
            server,
            "pub",
            subject,
            json.dumps(payload, separators=(",", ":")),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=5,
    )


def write_report(path: Path, report: dict) -> None:
    """Replace the final probe report atomically, preserving any prior file on failure."""
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            json.dump(report, temporary, indent=2, sort_keys=True)
            temporary.write("\n")
        os.replace(temporary_path, path)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--server", required=True)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--source", default="drone-1")
    parser.add_argument("--target", default="drone-2")
    parser.add_argument("--mutation", choices=sorted(SUPPORTED_MUTATIONS), default="wrong-callsign")
    parser.add_argument("--max-messages", type=int, default=MAX_PROBE_MESSAGES)
    parser.add_argument("--wait-timeout-seconds", type=float, default=90.0)
    args = parser.parse_args()
    validate_server(args.server)
    try:
        validate_vehicle_callsigns(args.source, args.target)
    except ValueError as error:
        raise SystemExit(str(error)) from error
    if args.max_messages != MAX_PROBE_MESSAGES:
        raise SystemExit("probe requires exactly one bounded message")
    if not 1 <= args.wait_timeout_seconds <= 300:
        raise SystemExit("pose wait timeout must be in 1..300 seconds")
    if args.run_dir.is_symlink():
        raise SystemExit("selected run directory is a symlink")

    run_dir = args.run_dir.resolve(strict=True)
    subject = f"furia.gzsitl.setpoint.{args.target}"
    pose_path = run_dir / "gazebo-pose.jsonl"
    report = {
        "contract": "damn-vulnerable-drone.swarm-cross-vehicle-probe",
        "version": "1.3.0",
        "runId": run_dir.name,
        "sourceVehicle": args.source,
        "targetVehicle": args.target,
        "subject": subject,
        "mutation": args.mutation,
        "status": "running",
        "publishedMessages": 0,
        "scope": "one live-setpoint mutation on the run-owned IPv4 loopback broker",
        "payloadCarriesExecutionGrant": False,
        "limits": "Local NATS client only; authority grants remain separate; no broker ACL or production/Core claim.",
    }
    output = run_dir / "research-cross-vehicle-authorization.json"
    write_report(output, report)
    try:
        validate_run_evidence_paths(run_dir, args.target)
        validate_run_nats_endpoint(
            args.server, run_dir, time.monotonic() + args.wait_timeout_seconds
        )
        model, target = target_plan(run_dir, args.target)
        config_text = read_run_text(run_dir, Path("research-swarm-config.json"))
        if config_text is None:
            raise ValueError("run-owned swarm config is unavailable")
        config = strict_json_loads(config_text)
        models = vehicle_models_from_config(config)
        validate_probe_roster(models, args.source, args.target)
        pose_deadline = time.monotonic() + args.wait_timeout_seconds
        dwell = None
        while time.monotonic() < pose_deadline:
            dwell = pose_window_at_target(pose_path, model, target)
            if dwell:
                break
            time.sleep(0.2)
        if dwell is None:
            raise TimeoutError(
                "independent Gazebo pose never held the target waypoint for the declared dwell"
            )
        report["preInjectionPoseWindow"] = dwell
        effect_before = vehicle_effect_snapshot(run_dir, models)
        report["effectSnapshotBefore"] = effect_before

        before = (
            read_run_text(
                run_dir,
                Path("logs") / f"antoine-controller-{args.target}.log",
                errors="replace",
            )
            or ""
        )
        captured = subprocess.run(
            [
                "nats",
                "--no-context",
                "--server",
                args.server,
                "sub",
                "--count=1",
                "--raw",
                subject,
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=8,
        )
        payload = read_one_json(captured.stdout)
        mutated, mutation_evidence = mutate_setpoint(
            payload, args.mutation, args.source, args.target, run_dir.name
        )
        if mutated is None:
            raise ValueError("requested supported mutation returned no payload")
        probe_sequence = mutated["sequence"]
        report.update(
            {
                "mutationSupport": mutation_evidence,
                "capturedSequence": payload["sequence"],
                "probeSequence": probe_sequence,
                "capturedPayloadSha256": hashlib.sha256(
                    json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
                ).hexdigest(),
                "mutatedPayloadSha256": hashlib.sha256(
                    json.dumps(mutated, sort_keys=True, separators=(",", ":")).encode()
                ).hexdigest(),
                "capturedSetpointHadRunIdField": "runId" in payload,
                "capturedTaskId": payload.get("taskId"),
                "capturedPositionEnuM": payload.get("position_enu_m"),
            }
        )
        publish_json(args.server, subject, mutated)
        report["publishedMessages"] = 1
        report["injectionAtUnixMs"] = int(time.time() * 1000)
        decision_deadline = time.monotonic() + DECISION_TIMEOUT_SECONDS
        report["decisionTimeoutSeconds"] = DECISION_TIMEOUT_SECONDS
        decision = None
        after = before
        while time.monotonic() < decision_deadline and decision is None:
            after = (
                read_run_text(
                    run_dir,
                    Path("logs") / f"antoine-controller-{args.target}.log",
                    errors="replace",
                )
                or ""
            )
            decision = controller_decision(
                args.mutation,
                after[len(before) :].splitlines(),
                probe_sequence,
                args.target,
                str(mutation_evidence.get("after", "")),
                report["mutatedPayloadSha256"],
            )
            if decision["status"] == "inconclusive":
                decision = None
                time.sleep(0.05)
        report["controllerDecision"] = decision or {"status": "inconclusive"}
        effect_at_decision = vehicle_effect_snapshot(run_dir, models)
        time.sleep(1.0)
        effect_after = vehicle_effect_snapshot(run_dir, models)
        report["effectSnapshotAtDecision"] = effect_at_decision
        report["effectSnapshotAfterObservation"] = effect_after
        report["effectComparison"] = compare_effect_snapshots(
            effect_before, effect_after, models, run_dir.name
        )
        report["controllerLogEvidence"] = [
            line
            for line in after[len(before) :].splitlines()
            if f"ANTOINE_GZ_SETPOINT_REJECTED callsign={args.target}" in line
            or f"ANTOINE_GZ_SETPOINT_RECEIVED callsign={args.target} sequence={probe_sequence} "
            in line
            or report["mutatedPayloadSha256"] in line
            or f"ANTOINE_GZ_SETPOINT_HOLD callsign={args.target}" in line
        ][-20:]
        if decision and decision["status"] in {
            "rejected",
            "rejected-before-active-setpoint",
        }:
            report["status"] = report["effectComparison"]["status"]
            if report["status"] == "measured":
                report["status"] = "passed"
        else:
            report["status"] = "inconclusive"
    except (OSError, ValueError, subprocess.SubprocessError, TimeoutError, KeyError) as error:
        report.update({"status": "inconclusive", "error": str(error)})
    write_report(output, report)
    print(output)
    return 0 if report.get("status") == "passed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
