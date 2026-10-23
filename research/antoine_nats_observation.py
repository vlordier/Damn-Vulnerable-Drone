#!/usr/bin/env python3
"""Bounded, read-only capture of selected subjects on one local proof broker."""

from __future__ import annotations

import argparse
import json
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from dvd_research.run_files import write_run_json
from dvd_research.run_nats import validate_run_nats_endpoint
from dvd_research.strict_json import loads as strict_json_loads

SUBJECTS = {
    "mission_setpoint": "furia.gzsitl.setpoint.drone-1",
    "execution_grant": "furia.prime.execution-grant.drone-1",
    "vehicle_state": "furia.gzsitl.state.drone-1",
    "vigie_admission": "brenta.swarm.0.vehicle.drone-1.action-outcome",
}
REDACT_TOKENS = ("signature", "seed", "secret", "credential", "privatekey")


def sanitize(value: Any) -> Any:
    if isinstance(value, dict):
        safe = {}
        for key, item in value.items():
            normalized = key.replace("_", "").replace("-", "").lower()
            if any(token in normalized for token in REDACT_TOKENS):
                safe[key] = "[redacted]"
            else:
                safe[key] = sanitize(item)
        return safe
    if isinstance(value, list):
        return [sanitize(item) for item in value]
    return value


def capture(server: str, name: str, subject: str) -> dict[str, Any]:
    started = int(time.time() * 1000)
    messages = []
    malformed = 0
    attempts = 1
    code = None
    timed_out = False
    stderr = ""
    start_error = None
    command = [
        "nats",
        "--no-context",
        "--server",
        server,
        "sub",
        "--raw",
        "--count=1",
        subject,
    ]
    try:
        completed = subprocess.run(command, text=True, capture_output=True, timeout=35, check=False)
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
        code = None
        timed_out = True
    except OSError as error:
        stdout = ""
        code = None
        timed_out = False
        start_error = f"{type(error).__name__}: {error}"[:500]
    else:
        stdout, stderr, code = completed.stdout, completed.stderr, completed.returncode
        timed_out = False

    for line in stdout.splitlines():
        if not line.strip():
            continue
        try:
            messages.append(sanitize(strict_json_loads(line)))
        except json.JSONDecodeError:
            malformed += 1

    # NATS CLI output alone is not enough to prove a readable publication: a
    # failed or timed-out subscriber may still have emitted partial stdout.
    valid_capture = (
        code == 0
        and not timed_out
        and start_error is None
        and len(messages) == 1
        and malformed == 0
    )
    return {
        "name": name,
        "subject": subject,
        "clientAuthentication": "none",
        "requestedMaxMessages": 1,
        "capturedMessages": len(messages),
        "captureValid": valid_capture,
        "subscriberAttempts": attempts,
        "messages": messages,
        "malformedOutputLines": malformed,
        "subscriberExitCode": code,
        "subscriberTimedOut": timed_out,
        "subscriberStartError": start_error,
        "stderr": stderr.strip()[:500],
        "captureStartedAtUnixMs": started,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--server", required=True)
    parser.add_argument("--run-dir", required=True, type=Path)
    args = parser.parse_args()
    try:
        parsed = urlsplit(args.server)
        port = parsed.port
    except ValueError as error:
        raise SystemExit("refusing an invalid NATS endpoint") from error
    if (
        parsed.scheme != "nats"
        or parsed.hostname != "127.0.0.1"
        or port is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
        or parsed.netloc != f"127.0.0.1:{port}"
    ):
        raise SystemExit("refusing any endpoint except this run's explicit IPv4 loopback NATS URL")

    try:
        validate_run_nats_endpoint(args.server, args.run_dir, time.monotonic() + 180)
    except (TimeoutError, ValueError) as error:
        raise SystemExit(str(error)) from error

    ready_deadline = time.monotonic() + 10
    broker_ready = False
    readiness_error = None
    while time.monotonic() < ready_deadline:
        try:
            ready = subprocess.run(
                ["nats", "--no-context", "--server", args.server, "rtt"],
                text=True,
                capture_output=True,
                timeout=3,
                check=False,
            )
        except OSError as error:
            readiness_error = f"NATS readiness command could not start ({type(error).__name__})"
            break
        if ready.returncode == 0:
            broker_ready = True
            break
        time.sleep(0.25)

    if not broker_ready and readiness_error is None:
        readiness_error = "the run-owned loopback NATS broker did not become ready"
    if readiness_error is None:
        results = []
        with ThreadPoolExecutor(max_workers=len(SUBJECTS)) as pool:
            futures = {
                pool.submit(capture, args.server, name, subject): name
                for name, subject in SUBJECTS.items()
            }
            for future in as_completed(futures):
                results.append(future.result())
    else:
        results = [
            {
                "name": name,
                "subject": subject,
                "clientAuthentication": "none",
                "requestedMaxMessages": 1,
                "capturedMessages": 0,
                "captureValid": False,
                "subscriberAttempts": 0,
                "messages": [],
                "malformedOutputLines": 0,
                "subscriberExitCode": None,
                "subscriberTimedOut": False,
                "subscriberStartError": None,
                "stderr": "",
                "skippedReason": readiness_error,
                "captureStartedAtUnixMs": int(time.time() * 1000),
            }
            for name, subject in SUBJECTS.items()
        ]
    results.sort(key=lambda item: item["name"])
    report = {
        "contract": "damn-vulnerable-drone.antoine-nats-observation",
        "version": "1.0.0",
        "scenario": "nats-readable-subjects-antoine",
        "runId": args.run_dir.name,
        "scope": "single run-owned loopback NATS broker; four exact subjects; at most one message per subject in a 35-second window",
        "server": args.server,
        "authenticationUsed": False,
        "brokerBoundary": {
            "bindAddress": "127.0.0.1",
            "authentication": "disabled",
            "accessClass": "same-host client with broker reachability",
            "authorization": "no per-subject ACL configured by this research profile",
            "remoteReachability": "not tested",
        },
        "redaction": "signature, seed, secret, credential, and private-key fields",
        "readinessError": readiness_error,
        "subjects": results,
        "interpretation": (
            "A local process able to reach this loopback broker read the selected public run, mission, "
            "grant, state, and admission fields without NATS credentials. Subject access does not "
            "itself grant actuator authority; the local actuator applier still enforces signatures and "
            "grant binding. This is a same-host exposure measurement, not a remote disclosure finding."
            if all(item["captureValid"] for item in results)
            else "Capture incomplete: one or more selected subjects had no readable messages. "
            "Do not draw a disclosure conclusion from this run."
        ),
    }
    output = write_run_json(args.run_dir, Path("research-nats-observation.json"), report)
    print(output)
    return 0 if all(item["captureValid"] for item in results) else 2


if __name__ == "__main__":
    raise SystemExit(main())
