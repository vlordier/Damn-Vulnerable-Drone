#!/usr/bin/env python3
"""Capture one state from each vehicle on an isolated two-drone NATS run."""

from __future__ import annotations

import argparse
import json
import math
import os
import subprocess
import time
from pathlib import Path

from dvd_research.run_files import read_run_text, write_run_json, write_run_text
from dvd_research.run_nats import validate_run_nats_endpoint

POSE_TOPIC = "/world/antoine_gz_flight_proof/pose/info"
POSE_MODELS = {"drone-1": "isr", "drone-2": "fpv-a"}


def gazebo_pose_samples(payload: str) -> list[dict]:
    """Extract complete, finite two-model poses from Gazebo JSON output."""
    decoder = json.JSONDecoder()
    samples = []
    cursor = 0
    while cursor < len(payload):
        start = payload.find("{", cursor)
        if start < 0:
            break
        try:
            message, end = decoder.raw_decode(payload, start)
        except json.JSONDecodeError:
            cursor = start + 1
            continue
        cursor = end
        if not isinstance(message, dict):
            continue
        header = message.get("header")
        stamp = header.get("stamp") if isinstance(header, dict) else None
        if not isinstance(stamp, dict):
            continue
        try:
            seconds = int(stamp.get("sec", 0))
            nanoseconds = int(stamp.get("nsec", 0))
        except (TypeError, ValueError, OverflowError):
            continue
        if seconds < 0 or not 0 <= nanoseconds < 1_000_000_000:
            continue
        poses = message.get("pose")
        if not isinstance(poses, list):
            continue
        by_model = {}
        for pose in poses:
            if not isinstance(pose, dict):
                continue
            model = str(pose.get("name", "")).split("::")[-1]
            position = pose.get("position")
            if model not in POSE_MODELS.values() or not isinstance(position, dict):
                continue
            coordinates = [position.get(axis) for axis in ("x", "y", "z")]
            if any(
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                for value in coordinates
            ):
                continue
            by_model[model] = coordinates
        if all(model in by_model for model in POSE_MODELS.values()):
            samples.append(
                {
                    "simulationTimeSeconds": seconds + nanoseconds / 1_000_000_000,
                    "positionByCallsign": {
                        callsign: by_model[model] for callsign, model in POSE_MODELS.items()
                    },
                }
            )
    return samples


def capture_gazebo_truth(gz: str, run_dir: Path, partition: str) -> dict:
    env = os.environ.copy()
    env["GZ_PARTITION"] = partition
    env["GZ_IP"] = "127.0.0.1"
    command = [
        gz,
        "topic",
        "--echo",
        "--json-output",
        "--num",
        "5",
        "--topic",
        POSE_TOPIC,
    ]
    try:
        completed = subprocess.run(
            command, text=True, capture_output=True, timeout=15, check=False, env=env
        )
    except subprocess.TimeoutExpired as error:
        stdout = (
            error.stdout.decode(errors="replace")
            if isinstance(error.stdout, bytes)
            else (error.stdout or "")
        )
        stderr = (
            error.stderr.decode(errors="replace")
            if isinstance(error.stderr, bytes)
            else (error.stderr or "")
        )
        if stdout:
            write_run_text(run_dir, Path("gazebo-pose-info.jsonl"), stdout)
        return {
            "status": "unavailable",
            "reason": (stderr.strip() or "Gazebo pose capture timed out")[:300],
        }
    if completed.stdout:
        write_run_text(run_dir, Path("gazebo-pose-info.jsonl"), completed.stdout)
    if completed.returncode != 0:
        return {
            "status": "unavailable",
            "reason": completed.stderr.strip()[:300] or "Gazebo pose subscriber failed",
        }
    samples = gazebo_pose_samples(completed.stdout)
    if (
        len(samples) < 2
        or samples[-1]["simulationTimeSeconds"] <= samples[-2]["simulationTimeSeconds"]
    ):
        return {
            "status": "inconclusive",
            "sampleCount": len(samples),
            "reason": "Gazebo did not provide two advancing complete two-model pose samples",
        }
    latest = samples[-1]
    first, second = (latest["positionByCallsign"][callsign] for callsign in POSE_MODELS)
    separation = math.dist(first, second)
    return {
        "status": "observed",
        "topic": POSE_TOPIC,
        "partition": partition,
        "sampleCount": len(samples),
        "simulationTimeSeconds": latest["simulationTimeSeconds"],
        "positionByCallsign": latest["positionByCallsign"],
        "separationM": round(separation, 3),
        "separationAtLeast5m": separation >= 5.0,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--gz", required=True)
    parser.add_argument("--server", required=True)
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--applier-log", required=True, type=Path)
    args = parser.parse_args()

    try:
        validate_run_nats_endpoint(args.server, args.run_dir, time.monotonic() + 20)
        run_dir = args.run_dir.absolute()
        applier_log = args.applier_log.absolute().relative_to(run_dir)
        applier_log_text = read_run_text(run_dir, applier_log, errors="replace")
        if applier_log_text is None:
            raise ValueError("run-owned actuator applier log is unavailable")
        profile_text = read_run_text(run_dir, Path("research-profile.env"))
        if profile_text is None:
            raise ValueError("run-owned Gazebo profile is unavailable")
        profile = dict(line.split("=", 1) for line in profile_text.splitlines() if "=" in line)
        partition = profile.get("GZ_PARTITION", "")
        if not partition or os.environ.get("GZ_PARTITION") != partition:
            raise ValueError("active Gazebo partition does not match the selected run")
        if profile.get("GZ_IP") != "127.0.0.1" or os.environ.get("GZ_IP") != "127.0.0.1":
            raise ValueError("smoke probe requires the run's local Gazebo discovery endpoint")
    except (OSError, ValueError, TimeoutError) as error:
        raise SystemExit(f"refusing unverified two-drone run inputs: {error}") from error

    vehicles = POSE_MODELS
    states = {}
    for callsign, model in vehicles.items():
        command = [
            "nats",
            "--no-context",
            "--server",
            args.server,
            "sub",
            "--raw",
            "--count=1",
            f"furia.gzsitl.state.{callsign}",
        ]
        try:
            completed = subprocess.run(
                command, text=True, capture_output=True, timeout=20, check=False
            )
        except subprocess.TimeoutExpired:
            states[callsign] = {"model": model, "received": False, "reason": "timeout"}
            continue
        if completed.returncode != 0:
            states[callsign] = {
                "model": model,
                "received": False,
                "reason": completed.stderr.strip()[:300] or "subscriber failed",
            }
            continue
        try:
            state = json.loads(next(line for line in completed.stdout.splitlines() if line.strip()))
        except (StopIteration, json.JSONDecodeError):
            states[callsign] = {"model": model, "received": False, "reason": "invalid JSON state"}
            continue
        states[callsign] = {
            "model": model,
            "received": state.get("identity") == callsign,
            "source": state.get("source"),
            "positionEnuM": state.get("position_enu_m"),
            "simulationTimeNs": state.get("simulation_time_ns"),
        }

    positions = [states.get(name, {}).get("positionEnuM") for name in vehicles]
    gazebo_truth = capture_gazebo_truth(args.gz, run_dir, partition)
    actuator_ready = {
        "drone-1": "startup-neutral-zero-published drone-1" in applier_log_text,
        "drone-2": "startup-neutral-zero-published drone-2" in applier_log_text,
    }
    separated = False
    if all(isinstance(point, list) and len(point) == 3 for point in positions):
        separated = sum((positions[0][i] - positions[1][i]) ** 2 for i in range(3)) ** 0.5 >= 5.0
    report = {
        "contract": "damn-vulnerable-drone.antoine-two-drone-smoke",
        "version": "1.0.0",
        "runId": run_dir.name,
        "scope": "two Gazebo X500 models, two mapped NATS state subjects, local loopback broker",
        "controllersStarted": False,
        "vehicles": states,
        "neutralActuatorStartupAttested": actuator_ready,
        "natsStateSeparationAtLeast5m": separated,
        "gazeboTruth": gazebo_truth,
        "initialSeparationAtLeast5m": separated and gazebo_truth.get("separationAtLeast5m") is True,
        "status": "passed"
        if all(item.get("received") for item in states.values())
        and all(actuator_ready.values())
        and separated
        and gazebo_truth.get("status") == "observed"
        and gazebo_truth.get("separationAtLeast5m") is True
        else "inconclusive",
        "limits": "Correlates callsign-bound NATS state with bounded independent Gazebo world poses and neutral actuator startup; does not prove two Antoine controllers or coordinated flight.",
    }
    output = write_run_json(run_dir, Path("research-two-drone-smoke.json"), report)
    print(output)
    return 0 if report["status"] == "passed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
