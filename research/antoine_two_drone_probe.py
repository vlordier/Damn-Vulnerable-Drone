#!/usr/bin/env python3
"""Capture one state from each vehicle on an isolated two-drone NATS run."""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path
from urllib.parse import urlsplit


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--server", required=True)
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--applier-log", required=True, type=Path)
    args = parser.parse_args()

    endpoint = urlsplit(args.server)
    if endpoint.scheme != "nats" or endpoint.hostname != "127.0.0.1" or endpoint.port is None:
        raise SystemExit("refusing anything except the run-owned IPv4 loopback NATS broker")

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
    applier_log = args.applier_log.read_text(errors="replace")
    actuator_ready = {
        "drone-1": "startup-neutral-zero-published drone-1" in applier_log,
        "drone-2": "startup-neutral-zero-published drone-2" in applier_log,
    }
    separated = False
    if all(isinstance(point, list) and len(point) == 3 for point in positions):
        separated = sum((positions[0][i] - positions[1][i]) ** 2 for i in range(3)) ** 0.5 >= 5.0
    report = {
        "contract": "damn-vulnerable-drone.antoine-two-drone-smoke",
        "version": "1.0.0",
        "runId": args.run_dir.name,
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
    output = args.run_dir / "research-two-drone-smoke.json"
    output.write_text(json.dumps(report, indent=2) + "\n")
    print(output)
    return 0 if report["status"] == "passed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
