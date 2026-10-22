#!/usr/bin/env python3
"""One-message, run-scoped waypoint mutation for the local Antoine/Gazebo lab."""

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

from dvd_research.run_files import read_run_text, write_run_json
from dvd_research.run_nats import validate_run_nats_endpoint

CONTROLLER_LOG = Path("logs/antoine-controller.log")
MOTOR_SEQUENCE = Path("applier-authorized/drone-1.motors")
ACTUATOR_AUDIT = Path("applier-neutral/actuator-output.jsonl")


def read_one_json(output: str) -> dict:
    for line in output.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    raise ValueError("NATS raw subscription did not return a JSON object")


def last_motor_sequence(run_dir: Path) -> int:
    try:
        value = read_run_text(run_dir, MOTOR_SEQUENCE)
    except OSError:
        return 0
    if value is None:
        return 0
    try:
        return int(value.strip())
    except ValueError:
        return 0


def wait_for_controller_ready(run_dir: Path, deadline: float) -> None:
    while time.monotonic() < deadline:
        try:
            log = read_run_text(run_dir, CONTROLLER_LOG, errors="replace") or ""
        except OSError:
            log = ""
        if (
            "ANTOINE_GZ_SETPOINT_RECEIVED callsign=drone-1" in log
            and last_motor_sequence(run_dir) > 10
        ):
            return
        time.sleep(0.1)
    raise TimeoutError("authorized setpoint and actuator traffic did not become ready")


def read_audit_rows(run_dir: Path) -> list[dict]:
    rows = []
    try:
        content = read_run_text(run_dir, ACTUATOR_AUDIT)
    except OSError:
        content = None
    if content is not None:
        for line in content.splitlines():
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                rows.append(row)
    return rows


def wait_for_mutation_effect(
    run_dir: Path,
    sequence: int,
    run_id: str,
    prior_audit_rows: int,
    deadline: float,
) -> tuple[bool, dict | None, int | None]:
    marker = (
        f"ANTOINE_GZ_SETPOINT_RECEIVED callsign=drone-1 sequence={sequence} accepted_at_unix_ms="
    )
    accepted = False
    accepted_observed_at = None
    neutral_stop = None
    while time.monotonic() < deadline:
        try:
            log_text = read_run_text(run_dir, CONTROLLER_LOG, errors="replace") or ""
            accepted_line = next((line for line in log_text.splitlines() if marker in line), None)
            if accepted_line:
                timestamp = accepted_line.partition(marker)[2].strip()
                if timestamp.isdigit():
                    accepted = True
                    if accepted_observed_at is None:
                        accepted_observed_at = int(timestamp)
        except OSError:
            pass
        rows = read_audit_rows(run_dir)
        new_rows = rows[prior_audit_rows:]
        neutral_stop = next(
            (
                row
                for row in new_rows
                if row.get("stage") == "explicit-stop"
                and row.get("neutral") is True
                and row.get("runId") == run_id
                and row.get("contract") == "furia.gzsitl.actuator-output-audit"
                and row.get("actuatorKind") == "motors"
                and row.get("vehicleId") == "drone-1"
                and row.get("authority") == "safe-stop"
            ),
            None,
        )
        if accepted and neutral_stop:
            break
        time.sleep(0.1)
    return accepted, neutral_stop, accepted_observed_at


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--server", required=True)
    parser.add_argument("--run-dir", required=True, type=Path)
    args = parser.parse_args()

    run_dir = args.run_dir
    base = {
        "scenario": "waypoint-injection-antoine",
        "scope": "single local run, loopback NATS, Gazebo X500",
        "server": args.server,
        "targetSubject": "furia.gzsitl.setpoint.drone-1",
        "publishedMessages": 1,
        "runId": run_dir.name,
        "mutation": "position_enu_m shifted +2m east; all captured grant/task/admission fields retained",
        "sequenceStrategy": "single high-water sequence to avoid being superseded by the local publisher",
    }
    try:
        deadline = time.monotonic() + 100
        validate_run_nats_endpoint(args.server, run_dir, deadline)
        run_id = run_dir.name
        wait_for_controller_ready(run_dir, deadline)
        subject = base["targetSubject"]
        captured = subprocess.run(
            ["nats", "--no-context", "--server", args.server, "sub", "--count=1", "--raw", subject],
            check=True,
            capture_output=True,
            text=True,
            timeout=12,
        )
        message = read_one_json(captured.stdout)
        expected_binding = {
            "contract": "furia.gzsitl.mission-setpoint",
            "version": "2.1.0",
            "vehicleId": "drone-1",
            "missionId": "antoine-local-authority-drone-1",
            "graphId": "antoine-local-authority-graph-drone-1",
            "taskId": "antoine-local-authority-waypoint-drone-1",
        }
        mismatched_binding = [
            field for field, expected in expected_binding.items() if message.get(field) != expected
        ]
        if mismatched_binding:
            raise ValueError(
                "captured setpoint does not match the run's local fixture binding: "
                + ", ".join(mismatched_binding)
            )
        original_position = message.get("position_enu_m")
        if not isinstance(original_position, list) or len(original_position) != 3:
            raise ValueError("captured setpoint has no three-axis position_enu_m")
        original_position = [float(value) for value in original_position]
        mutated_position = [original_position[0] + 2.0, original_position[1], original_position[2]]
        prior_sequence = int(message.get("sequence", 0))
        attack_sequence = max(prior_sequence + 1, 1_000_000)
        now_ms = int(time.time() * 1000)
        message["position_enu_m"] = mutated_position
        message["sequence"] = attack_sequence
        message["issuedAtMs"] = now_ms
        message["expiresAtMs"] = now_ms + 1_500

        prior_audit_rows = len(read_audit_rows(run_dir))
        publish_started_at = int(time.time() * 1000)
        published = subprocess.run(
            [
                "nats",
                "--no-context",
                "--server",
                args.server,
                "pub",
                subject,
                json.dumps(message, separators=(",", ":")),
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=8,
        )
        accepted, neutral_stop, accepted_observed_at = wait_for_mutation_effect(
            run_dir,
            attack_sequence,
            run_id,
            prior_audit_rows,
            time.monotonic() + 8,
        )
        neutralized = neutral_stop is not None
        stop_at = neutral_stop.get("recordedAtUnixMs") if neutral_stop else None
        stop_matches_run = bool(
            neutral_stop
            and neutral_stop.get("runId") == run_id
            and neutral_stop.get("contract") == "furia.gzsitl.actuator-output-audit"
            and neutral_stop.get("actuatorKind") == "motors"
            and neutral_stop.get("vehicleId") == "drone-1"
            and neutral_stop.get("authority") == "safe-stop"
            and neutral_stop.get("neutral") is True
        )
        causal_order_observed = bool(
            accepted and stop_matches_run and type(stop_at) is int and publish_started_at <= stop_at
        )
        temporal_evidence = "mutation accepted in controller log; same-run neutral motor stop recorded after publish"
        base.update(
            {
                "status": "observed"
                if accepted and neutralized and causal_order_observed
                else "inconclusive",
                "capturedSequence": prior_sequence,
                "injectedSequence": attack_sequence,
                "originalPositionEnuM": original_position,
                "mutatedPositionEnuM": mutated_position,
                "capturedSetpointHadSignatureField": any(
                    "signature" in key.lower() for key in message
                ),
                "capturedSetpointHadRunIdField": "runId" in message,
                "runBindingBasis": "run-owned loopback broker, controller readiness, and local fixture mission/task identity",
                "runId": run_id,
                "setpointParserAcceptedMutation": accepted,
                "neutralActuatorOutputObserved": neutralized,
                "attackPublishStartedAtUnixMs": publish_started_at,
                "parserAcceptanceObservedAtUnixMs": accepted_observed_at,
                "parserAcceptanceDetectedAtUnixMs": int(time.time() * 1000) if accepted else None,
                "postAttackNeutralStop": neutral_stop,
                "expectedRunId": run_id,
                "neutralStopMatchedRun": stop_matches_run,
                "causalEvidenceBasis": temporal_evidence if causal_order_observed else None,
                "causalOrderObserved": causal_order_observed,
                "publishToStopMs": stop_at - publish_started_at if stop_at else None,
                "publisherOutput": published.stdout.strip(),
                "observedAtUnixMs": int(time.time() * 1000),
            }
        )
        write_run_json(run_dir, Path("research-waypoint-injection.json"), base)
        print(json.dumps(base, indent=2))
        return 0 if accepted and neutralized and causal_order_observed else 2
    except Exception as error:  # persist partial evidence for failed/inconclusive attempts
        base.update({"status": "inconclusive", "error": f"{type(error).__name__}: {error}"})
        write_run_json(run_dir, Path("research-waypoint-injection.json"), base)
        print(json.dumps(base, indent=2), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
