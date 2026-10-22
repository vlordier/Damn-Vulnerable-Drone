#!/usr/bin/env python3
"""Replay one live, same-run setpoint once and record sequence rejection."""

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

from antoine_setpoint_mutation import (
    CONTROLLER_LOG,
    read_one_json,
    wait_for_controller_ready,
)
from dvd_research.run_files import read_run_text, write_run_json
from dvd_research.run_nats import validate_run_nats_endpoint


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--server", required=True)
    parser.add_argument("--run-dir", required=True, type=Path)
    args = parser.parse_args()
    run_dir = args.run_dir
    subject = "furia.gzsitl.setpoint.drone-1"
    report = {
        "contract": "damn-vulnerable-drone.antoine-waypoint-replay-attempt",
        "version": "1.0.0",
        "scenario": "same-run-setpoint-replay-antoine",
        "runId": run_dir.name,
        "scope": "one captured message replayed once on run-owned loopback NATS",
        "subject": subject,
        "clientAuthentication": "none",
        "publishedMessages": 1,
    }
    try:
        deadline = time.monotonic() + 100
        validate_run_nats_endpoint(args.server, run_dir, deadline)
        wait_for_controller_ready(run_dir, deadline)
        log = read_run_text(run_dir, CONTROLLER_LOG, errors="replace") or ""

        before_rejects = log.count(
            "ANTOINE_GZ_SETPOINT_REJECTED callsign=drone-1 reason=vehicle-or-sequence"
        )
        captured = subprocess.run(
            ["nats", "--no-context", "--server", args.server, "sub", "--count=1", "--raw", subject],
            text=True,
            capture_output=True,
            timeout=8,
            check=True,
        )
        payload = read_one_json(captured.stdout)
        expected_binding = {
            "contract": "furia.gzsitl.mission-setpoint",
            "version": "2.1.0",
            "vehicleId": "drone-1",
            "missionId": "antoine-local-authority-drone-1",
            "graphId": "antoine-local-authority-graph-drone-1",
            "taskId": "antoine-local-authority-waypoint-drone-1",
        }
        mismatched_binding = [
            field for field, expected in expected_binding.items() if payload.get(field) != expected
        ]
        if mismatched_binding:
            raise ValueError(
                "captured setpoint does not match the run's local fixture binding: "
                + ", ".join(mismatched_binding)
            )

        report.update(
            {
                "capturedSequence": payload.get("sequence"),
                "capturedSetpointHadRunIdField": "runId" in payload,
                "runBindingBasis": "run-owned loopback broker, controller readiness, and local fixture mission/task identity",
                "capturedPositionEnuM": payload.get("position_enu_m"),
                "capturedExpiresAtMs": payload.get("expiresAtMs"),
                "captureAtUnixMs": int(time.time() * 1000),
            }
        )
        replay_published_at = int(time.time() * 1000)
        subprocess.run(
            [
                "nats",
                "--no-context",
                "--server",
                args.server,
                "pub",
                subject,
                json.dumps(payload, separators=(",", ":")),
            ],
            text=True,
            capture_output=True,
            timeout=8,
            check=True,
        )

        reject_marker = "ANTOINE_GZ_SETPOINT_REJECTED callsign=drone-1 reason=vehicle-or-sequence"
        rejected_at = None
        while time.monotonic() < deadline:
            try:
                current_log = read_run_text(run_dir, CONTROLLER_LOG, errors="replace") or ""
            except OSError:
                current_log = ""
            if current_log.count(reject_marker) > before_rejects:
                rejected_at = int(time.time() * 1000)
                break
            time.sleep(0.05)
        report.update(
            {
                "replayPublishedAtUnixMs": replay_published_at,
                "antiReplayRejectionObserved": rejected_at is not None,
                "rejectionObservedAtUnixMs": rejected_at,
                "result": "rejected" if rejected_at else "inconclusive",
            }
        )
        write_run_json(run_dir, Path("research-waypoint-replay.json"), report)
        print(json.dumps(report, indent=2))
        return 0 if rejected_at else 2
    except Exception as error:
        report.update({"result": "inconclusive", "error": f"{type(error).__name__}: {error}"})
        write_run_json(run_dir, Path("research-waypoint-replay.json"), report)
        print(json.dumps(report, indent=2), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
