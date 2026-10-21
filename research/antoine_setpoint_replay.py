#!/usr/bin/env python3
"""Replay one live, same-run setpoint once and record sequence rejection."""

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit


def validate_server(server: str, run_dir: Path, deadline: float) -> None:
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
        raise ValueError("refusing any NATS endpoint except an explicit IPv4 loopback URL")
    log_path = run_dir / "logs" / "nats.log"
    ready_line = f"Listening for client connections on 127.0.0.1:{parsed.port}"
    while time.monotonic() < deadline:
        try:
            log = log_path.read_text(errors="replace")
        except OSError:
            log = ""
        if ready_line in log and "Server is ready" in log:
            return
        time.sleep(0.1)
    raise TimeoutError("run-owned NATS broker did not confirm the requested loopback endpoint")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--server", required=True)
    parser.add_argument("--run-dir", required=True, type=Path)
    args = parser.parse_args()
    run_dir = args.run_dir
    controller_log = run_dir / "logs" / "antoine-controller.log"
    motor_file = run_dir / "applier-authorized" / "drone-1.motors"
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
        validate_server(args.server, run_dir, deadline)
        while time.monotonic() < deadline:
            try:
                log = controller_log.read_text(errors="replace")
                motor_sequence = int(motor_file.read_text().strip())
            except (OSError, ValueError):
                log, motor_sequence = "", 0
            if "ANTOINE_GZ_SETPOINT_RECEIVED callsign=drone-1" in log and motor_sequence > 10:
                break
            time.sleep(0.1)
        else:
            raise TimeoutError("controller did not accept a live setpoint and actuator commands")

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
        payload = None
        for line in captured.stdout.splitlines():
            try:
                candidate = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(candidate, dict):
                payload = candidate
                break
        if payload is None:
            raise ValueError("could not decode one live JSON setpoint")
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
                current_log = controller_log.read_text(errors="replace")
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
        (run_dir / "research-waypoint-replay.json").write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps(report, indent=2))
        return 0 if rejected_at else 2
    except Exception as error:
        report.update({"result": "inconclusive", "error": f"{type(error).__name__}: {error}"})
        (run_dir / "research-waypoint-replay.json").write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps(report, indent=2), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
