#!/usr/bin/env python3
"""Capture one state from each vehicle on an isolated two-drone NATS run."""

from __future__ import annotations

import argparse
import json
import subprocess
import time
from pathlib import Path

from dvd_research.run_files import read_run_text, write_run_json
from dvd_research.run_nats import validate_run_nats_endpoint


def main() -> int:
    parser = argparse.ArgumentParser()
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
    except (OSError, ValueError, TimeoutError) as error:
        raise SystemExit(f"refusing unverified two-drone run inputs: {error}") from error

    vehicles = {"drone-1": "isr", "drone-2": "fpv-a"}
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
        "initialSeparationAtLeast5m": separated,
        "status": "passed"
        if all(item.get("received") for item in states.values())
        and all(actuator_ready.values())
        and separated
        else "inconclusive",
        "limits": "Proves two isolated simulated models, state streams, and neutral actuator startup only; does not prove two Antoine controllers or coordinated flight.",
    }
    output = write_run_json(run_dir, Path("research-two-drone-smoke.json"), report)
    print(output)
    return 0 if report["status"] == "passed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
