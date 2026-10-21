#!/usr/bin/env python3
"""Correlate a bounded controller-only state partition with independent flight evidence."""

from __future__ import annotations

import json
import math
import re
import sys
from pathlib import Path
from typing import Any

MAX_DROPPED_MESSAGES = 200
MAX_POSE_TIME_SKEW_SECONDS = 0.15
MAX_PARTITION_WINDOW_SECONDS = 10.0
MINIMUM_SEPARATION_METERS = 1.0


def marker(line: str, name: str) -> dict[str, str] | None:
    match = re.search(rf"{re.escape(name)}\s+(.*)$", line)
    if not match:
        return None
    return dict(re.findall(r"([A-Za-z][A-Za-z0-9_]*)=([^\s]+)", match.group(1)))


def read_poses(run_dir: Path, models: set[str]) -> dict[str, list[tuple[float, list[float]]]]:
    result = {model: [] for model in models}
    try:
        with (run_dir / "gazebo-pose.jsonl").open(encoding="utf-8") as stream:
            for line in stream:
                try:
                    frame = json.loads(line)
                    stamp = frame["header"]["stamp"]
                    sim_time = float(stamp["sec"]) + float(stamp["nsec"]) / 1e9
                except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                    continue
                if not math.isfinite(sim_time):
                    continue
                for pose in frame.get("pose", []):
                    name = pose.get("name") if isinstance(pose, dict) else None
                    if name not in models:
                        continue
                    try:
                        point = [float(pose["position"][axis]) for axis in ("x", "y", "z")]
                    except (KeyError, TypeError, ValueError):
                        continue
                    if all(math.isfinite(value) for value in point):
                        result[name].append((sim_time, point))
    except OSError:
        pass
    return result


def nearest(
    samples: list[tuple[float, list[float]]], target: float
) -> tuple[float, list[float]] | None:
    return min(samples, key=lambda row: abs(row[0] - target)) if samples else None


def pose_window(samples: list[tuple[float, list[float]]], target: float) -> dict[str, Any] | None:
    row = nearest(samples, target)
    if row is None or abs(row[0] - target) > MAX_POSE_TIME_SKEW_SECONDS:
        return None
    return {
        "simulationTimeSeconds": row[0],
        "targetTimeSeconds": target,
        "timeSkewSeconds": round(abs(row[0] - target), 6),
        "positionEnuM": row[1],
    }


def actuator_vector(row: dict[str, Any] | None) -> list[float] | None:
    values = row.get("values") if isinstance(row, dict) else None
    if not isinstance(values, dict):
        return None
    flattened: list[float] = []
    for key in sorted(values):
        value = values[key]
        if isinstance(value, list):
            candidates = value
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            candidates = [value]
        else:
            continue
        if any(
            not isinstance(item, (int, float))
            or isinstance(item, bool)
            or not math.isfinite(float(item))
            for item in candidates
        ):
            return None
        flattened.extend(float(item) for item in candidates)
    return flattened or None


def authority_progress_during_partition(
    run_dir: Path, callsign: str, run_id: str, start_unix_ms: int, end_unix_ms: int
) -> dict[str, Any]:
    path = run_dir / f"flight-evidence-{callsign}.json"
    try:
        evidence = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"status": "unavailable", "eventCount": 0, "eventSequences": []}
    if (
        not isinstance(evidence, dict)
        or evidence.get("runId") != run_id
        or evidence.get("callsign") != callsign
    ):
        return {"status": "invalid", "eventCount": 0, "eventSequences": []}
    events = evidence.get("missionProgress")
    if not isinstance(events, list):
        return {"status": "unavailable", "eventCount": 0, "eventSequences": []}
    selected = [
        event
        for event in events
        if isinstance(event, dict)
        and event.get("runId") == run_id
        and event.get("callsign") == callsign
        and type(event.get("atUnixMs")) is int
        and start_unix_ms <= event["atUnixMs"] <= end_unix_ms
    ]
    sequences = [event.get("eventSequence") for event in selected]
    valid = bool(selected) and all(type(value) is int and value > 0 for value in sequences)
    valid = valid and sequences == sorted(set(sequences))
    return {
        "status": "measured" if valid else "inconclusive",
        "eventCount": len(selected),
        "eventSequences": sequences,
        "events": selected,
    }


def summarize(run_dir: Path) -> dict[str, Any]:
    config = json.loads((run_dir / "research-swarm-config.json").read_text(encoding="utf-8"))
    run_id = run_dir.name
    target = config.get("faultCallsign", "drone-2")
    fault = config.get("fault")
    if fault not in {"state-stream-partition", "controller-state-blackhole"}:
        fault = "state-stream-partition"
    roster = {item["callsign"]: item["model"] for item in config.get("vehicles", [])}
    if target not in roster or len(roster) < 2:
        raise ValueError("fault target and at least one peer must be present in the run roster")
    controller_log = run_dir / "logs" / f"antoine-controller-{target}.log"
    lines = controller_log.read_text(encoding="utf-8", errors="replace").splitlines()
    entered = [
        value for line in lines if (value := marker(line, "ANTOINE_GZ_STATE_PARTITION_ENTER"))
    ]
    exited = [value for line in lines if (value := marker(line, "ANTOINE_GZ_STATE_PARTITION_EXIT"))]
    hold_enter = [
        line for line in lines if f"ANTOINE_GZ_STALE_HOLD_ENTER callsign={target}" in line
    ]
    hold_exit = [line for line in lines if f"ANTOINE_GZ_STALE_HOLD_EXIT callsign={target}" in line]
    if len(entered) != 1 or len(exited) != 1:
        raise ValueError("partition entry and recovery markers must each be unique")
    start, end = entered[0], exited[0]
    if (
        start.get("run_id") != run_id
        or end.get("run_id") != run_id
        or start.get("callsign") != target
        or end.get("callsign") != target
    ):
        raise ValueError("partition markers do not match run/callsign")
    dropped = int(end["dropped_messages"])
    first_sequence = int(start["source_sequence"])
    last_sequence = int(end["last_dropped_source_sequence"])
    recovery_sequence = int(end["recovery_source_sequence"])
    first_sim = int(start["simulation_time_ns"])
    last_sim = int(end["last_dropped_simulation_time_ns"])
    recovery_sim = int(end["recovery_simulation_time_ns"])
    start_unix, end_unix = int(start["unix_ms"]), int(end["unix_ms"])
    after_ms, duration_ms = int(start["after_ms"]), int(start["duration_ms"])
    valid_relay = (
        1 <= dropped <= MAX_DROPPED_MESSAGES
        and first_sequence <= last_sequence < recovery_sequence
        and first_sim <= last_sim < recovery_sim
        and 250 <= after_ms <= 120_000
        and 250 <= duration_ms <= 10_000
        and 0 < end_unix - start_unix <= MAX_PARTITION_WINDOW_SECONDS * 1000
    )
    config_fault_call = target
    models = set(roster.values())
    poses = read_poses(run_dir, models)
    before_time = max(0.0, first_sim / 1e9 - 0.25)
    partition_time = last_sim / 1e9
    recovery_time = recovery_sim / 1e9
    windows: dict[str, dict[str, Any]] = {}
    for callsign, model in roster.items():
        samples = poses.get(model, [])
        before = pose_window(samples, before_time)
        during = pose_window(samples, partition_time)
        recovered = pose_window(samples, recovery_time)
        displacement = (
            math.dist(before["positionEnuM"], recovered["positionEnuM"])
            if before and recovered
            else None
        )
        windows[callsign] = {
            "model": model,
            "beforePartition": before,
            "partitionEnd": during,
            "afterRecovery": recovered,
            "poseDisplacementM": round(displacement, 4) if displacement is not None else None,
            "poseEvidenceComplete": all(item is not None for item in (before, during, recovered)),
        }
    pair_separations = []
    if all(windows[callsign]["poseEvidenceComplete"] for callsign in roster):
        for key in ("beforePartition", "partitionEnd", "afterRecovery"):
            positions = [windows[callsign][key]["positionEnuM"] for callsign in roster]
            for i, first in enumerate(positions):
                for second in positions[i + 1 :]:
                    pair_separations.append(math.dist(first, second))
    min_separation = min(pair_separations) if pair_separations else None
    times = {
        "before": start_unix - 100,
        "during": (start_unix + end_unix) // 2,
        "recovered": end_unix + 100,
    }
    actuator_windows: dict[str, dict[str, Any]] = {}
    actuator_path = run_dir / "applier-neutral" / "actuator-output.jsonl"
    records = {callsign: [] for callsign in roster}
    try:
        with actuator_path.open(encoding="utf-8") as stream:
            for line in stream:
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                callsign = row.get("vehicleId")
                if (
                    callsign in records
                    and row.get("runId") == run_id
                    and type(row.get("recordedAtUnixMs")) is int
                    and type(row.get("sequence")) is int
                ):
                    records[callsign].append(row)
    except OSError:
        pass
    for callsign, rows in records.items():
        rows.sort(key=lambda item: item["recordedAtUnixMs"])
        selected = {}
        for label, timestamp in times.items():
            row = min(
                rows, key=lambda item: abs(item["recordedAtUnixMs"] - timestamp), default=None
            )
            selected[label] = (
                row if row and abs(row["recordedAtUnixMs"] - timestamp) <= 1000 else None
            )
        ordered = [selected[key] for key in ("before", "during", "recovered")]
        valid = all(row is not None and row.get("vehicleId") == callsign for row in ordered)
        if valid:
            sequences = [row["sequence"] for row in ordered]
            valid = sequences[0] < sequences[1] < sequences[2]
        vectors = [actuator_vector(row) for row in ordered]
        valid = valid and all(vector is not None for vector in vectors)
        deltas = (
            [
                round(math.dist(vectors[0], vector), 6)
                if vectors[0] is not None and vector is not None and len(vectors[0]) == len(vector)
                else None
                for vector in vectors
            ]
            if all(vector is not None for vector in vectors)
            else [None, None, None]
        )
        window_evidence = {}
        for label, row, vector, delta in zip(
            ("beforePartition", "duringPartition", "afterRecovery"),
            ordered,
            vectors,
            deltas,
            strict=True,
        ):
            window_evidence[label] = {
                "record": row,
                "actuatorVector": vector,
                "deltaFromBeforeL2": delta,
            }
        actuator_windows[callsign] = {
            **window_evidence,
            "identitySequenceAndValuesValid": valid,
        }
    authority_progress = authority_progress_during_partition(
        run_dir, target, run_id, start_unix, end_unix
    )
    authority_continued = authority_progress["status"] == "measured"
    watchdog_recovered = bool(hold_enter and hold_exit)
    pose_complete = all(value["poseEvidenceComplete"] for value in windows.values())
    actuator_complete = all(
        value["identitySequenceAndValuesValid"] for value in actuator_windows.values()
    )
    status = (
        "passed"
        if (
            valid_relay
            and watchdog_recovered
            and authority_continued
            and pose_complete
            and actuator_complete
            and min_separation is not None
            and min_separation >= MINIMUM_SEPARATION_METERS
        )
        else "inconclusive"
    )
    return {
        "contract": "damn-vulnerable-drone.antoine-state-partition-evidence",
        "version": "1.1.0",
        "runId": run_id,
        "fault": fault,
        "status": status,
        "mechanism": "run-local controller-only NATS state subject relay; authority remains subscribed to original Gazebo state subject",
        "targetCallsign": config_fault_call,
        "partition": {
            "afterMs": after_ms,
            "durationMs": duration_ms,
            "droppedMessageCap": MAX_DROPPED_MESSAGES,
            "droppedMessages": dropped,
            "firstSourceSequence": first_sequence,
            "lastDroppedSourceSequence": last_sequence,
            "recoverySourceSequence": recovery_sequence,
            "firstDroppedSimulationTimeSeconds": first_sim / 1e9,
            "lastDroppedSimulationTimeSeconds": last_sim / 1e9,
            "recoverySimulationTimeSeconds": recovery_sim / 1e9,
            "startedAtUnixMs": start_unix,
            "recoveredAtUnixMs": end_unix,
            "sourceSequenceGap": recovery_sequence - last_sequence,
            "sourceSimulationGapSeconds": (recovery_sim - last_sim) / 1e9,
            "boundedAndRecovered": valid_relay,
        },
        "markers": {
            "partitionEntered": len(entered) == 1,
            "partitionExited": len(exited) == 1,
            "watchdogEntered": bool(hold_enter),
            "watchdogExited": bool(hold_exit),
            "authoritySourceContinued": authority_continued,
            "authorityProgress": authority_progress,
        },
        "vehicles": windows,
        "actuatorWindows": actuator_windows,
        "minimumSynchronizedWindowSeparationM": round(min_separation, 4)
        if min_separation is not None
        else None,
        "minimumSeparationThresholdM": MINIMUM_SEPARATION_METERS,
        "poseTimeToleranceSeconds": MAX_POSE_TIME_SKEW_SECONDS,
        "actuatorTimeToleranceMs": 1000,
        "actuatorDeltaMetric": "L2 distance over sorted numeric values fields; diagnostic only, not a neutralization claim",
        "limits": "Local simulated controller delivery partition only. Zero-velocity watchdog hold is not actuator neutralization; final disarm is qualified separately by the whole-flight report.",
    }


def main() -> int:
    if len(sys.argv) != 2:
        raise SystemExit("usage: summarize_swarm_state_partition.py RUN_DIR")
    run_dir = Path(sys.argv[1]).resolve(strict=True)
    report = summarize(run_dir)
    output = run_dir / "research-swarm-fault.json"
    output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(output)
    return 0 if report["status"] == "passed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
