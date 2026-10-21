#!/usr/bin/env python3
"""Inspect and safely stop a QGC/Gazebo run using its run-owned receipt."""

from __future__ import annotations

import argparse
import json
import math
import os
import signal
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

STOP_TIMEOUT_SECONDS = 5.0
STOP_POLL_SECONDS = 0.1
TELEMETRY_TIMEOUT_SECONDS = 1.5


def process_identity(pid: int) -> str | None:
    result = subprocess.run(
        ["ps", "-p", str(pid), "-o", "lstart=", "-o", "command="],
        capture_output=True,
        text=True,
        check=False,
    )
    value = result.stdout.strip()
    return value if result.returncode == 0 and value else None


def sample_telemetry(receipt: dict[str, Any]) -> dict[str, Any]:
    """Read one newly published state from this run's loopback NATS broker."""
    callsign = receipt.get("callsign")
    port = receipt.get("ports", {}).get("nats")
    run_directory = receipt.get("runDirectory")
    run_id = receipt.get("runId")
    if (
        not isinstance(callsign, str)
        or not callsign
        or not isinstance(port, int)
        or isinstance(port, bool)
        or not 1 <= port <= 65535
        or not isinstance(run_directory, str)
        or not isinstance(run_id, str)
    ):
        return {
            "status": "unavailable",
            "reason": "receipt lacks valid run, callsign, or NATS port",
        }
    run_path = Path(run_directory)
    if run_path.name != run_id or not run_path.is_dir():
        return {
            "status": "unavailable",
            "reason": "run directory is missing or does not match runId",
        }
    nats_log = run_path / "logs" / "nats.log"
    try:
        log = nats_log.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {"status": "unavailable", "reason": "run-owned NATS log is unavailable"}
    if (
        f"Listening for client connections on 127.0.0.1:{port}" not in log
        or "Server is ready" not in log
    ):
        return {
            "status": "unavailable",
            "reason": "run-owned NATS did not confirm its loopback endpoint",
        }

    try:
        result = subprocess.run(
            [
                "nats",
                "--no-context",
                "--server",
                f"nats://127.0.0.1:{port}",
                "sub",
                "--count=2",
                "--raw",
                f"furia.telemetry.{callsign}",
            ],
            capture_output=True,
            text=True,
            timeout=TELEMETRY_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {
            "status": "unavailable",
            "reason": "no fresh telemetry arrived before the bounded deadline",
        }
    if result.returncode != 0:
        return {"status": "unavailable", "reason": "NATS telemetry subscription failed"}
    try:
        messages = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
    except (json.JSONDecodeError, TypeError):
        return {"status": "invalid", "reason": "NATS returned a non-JSON telemetry message"}
    if len(messages) < 2 or not all(isinstance(message, dict) for message in messages):
        return {"status": "invalid", "reason": "NATS did not return two JSON telemetry samples"}
    for message in messages:
        if (
            message.get("schema") != "furia.telemetry"
            or message.get("version") != "1.0.0"
            or message.get("identity") != callsign
            or message.get("protocol") not in {"antoine", "antoine-gz"}
        ):
            return {
                "status": "invalid",
                "reason": "telemetry schema, protocol, or callsign does not match this profile",
            }
        simulation_time = message.get("time_s")
        if (
            not isinstance(simulation_time, (int, float))
            or isinstance(simulation_time, bool)
            or not math.isfinite(simulation_time)
        ):
            return {"status": "invalid", "reason": "telemetry has no finite simulation timestamp"}
    first_time, latest_time = messages[-2]["time_s"], messages[-1]["time_s"]
    if latest_time <= first_time:
        return {
            "status": "stale",
            "reason": "simulation time did not advance across fresh telemetry samples",
        }
    message = messages[-1]
    return {
        "status": "fresh",
        "observedAtUtc": datetime.now(UTC).isoformat(),
        "source": "newly received message on the run-owned loopback NATS subject",
        "subject": f"furia.telemetry.{callsign}",
        "schema": message["schema"],
        "version": message["version"],
        "protocol": message["protocol"],
        "simulationTimeSeconds": message["time_s"],
        "simulationTimeAdvanceSeconds": message["time_s"] - first_time,
        "sampleCount": len(messages),
        "armed": message.get("armed") if isinstance(message.get("armed"), bool) else None,
        "mode": message.get("mode"),
    }


def inspect(receipt_path: Path) -> dict[str, Any]:
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    checks: dict[str, Any] = {}
    for name, process in receipt.get("processes", {}).items():
        pid = process.get("pid")
        actual = process_identity(pid) if isinstance(pid, int) and pid > 1 else None
        checks[name] = {
            "pid": pid,
            "alive": actual is not None,
            "identityMatches": actual == process.get("identity") if actual else False,
        }
    ports = receipt.get("ports", {})
    for name, protocol in (("nats", "TCP"), ("qgcUdpListen", "UDP"), ("bridgeUdpBind", "UDP")):
        port = ports.get(name)
        if isinstance(port, int):
            selector = f"-i{protocol}:{port}"
            command = ["lsof", "-nP", selector]
            if protocol == "TCP":
                command.extend(["-sTCP:LISTEN"])
            probe = subprocess.run(command, capture_output=True, text=True, check=False)
            checks[f"{name}Port"] = {
                "port": port,
                "protocol": protocol,
                "listening": probe.returncode == 0,
            }
    run_id = receipt.get("runId")
    run_directory = receipt.get("runDirectory")
    stale = (
        not isinstance(run_id, str)
        or not run_id
        or not isinstance(run_directory, str)
        or Path(run_directory).name != run_id
        or not Path(run_directory).is_dir()
    )
    foreign = any(
        item["alive"] and not item["identityMatches"]
        for item in checks.values()
        if isinstance(item, dict) and "pid" in item
    )
    missing_owned = any(
        not item["alive"]
        for name, item in checks.items()
        if isinstance(item, dict) and "pid" in item and not name.lower().startswith("qgc")
    )
    telemetry = sample_telemetry(receipt)
    bridge = checks.get("mavlinkBridge", {})
    bridge_identity = receipt.get("processes", {}).get("mavlinkBridge", {}).get("identity", "")
    expected_qgc_peer = f"127.0.0.1:{receipt.get('ports', {}).get('qgcUdpListen')}"
    expected_bridge_bind = f"127.0.0.1:{receipt.get('ports', {}).get('bridgeUdpBind')}"
    routing_configured = (
        bridge.get("identityMatches") is True
        and isinstance(bridge_identity, str)
        and f"--qgc-peer {expected_qgc_peer}" in bridge_identity
        and f"--qgc-bind {expected_bridge_bind}" in bridge_identity
    )
    if telemetry.get("status") != "fresh":
        overall_status = "stale" if stale else "partial"
    elif stale or foreign:
        overall_status = "stale" if stale else "partial"
    elif missing_owned:
        overall_status = "partial"
    else:
        overall_status = "recorded"
    return {
        "contract": "damn-vulnerable-drone.interactive-profile-status",
        "version": "1.0.0",
        "observedAtUtc": datetime.now(UTC).isoformat(),
        "runId": receipt.get("runId"),
        "runDirectory": receipt.get("runDirectory"),
        "callsign": receipt.get("callsign"),
        "ports": receipt.get("ports"),
        "receiptStatus": receipt.get("status"),
        "processes": checks,
        "status": overall_status,
        "telemetry": telemetry,
        "qgcPeerRouting": {
            "status": "configured-unverified" if routing_configured else "unverified",
            "reason": "bridge command identity pins the peer, but a QGC-originated heartbeat is not observed by status",
        },
        "armed": telemetry.get("armed") if telemetry.get("status") == "fresh" else None,
        "armedReason": (
            "from fresh callsign-bound Antoine telemetry"
            if telemetry.get("status") == "fresh" and telemetry.get("armed") is not None
            else "fresh callsign-bound telemetry did not provide a boolean armed state"
        ),
    }


def stop_owned(receipt_path: Path) -> dict[str, Any]:
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    results = {}
    for name, process in receipt.get("processes", {}).items():
        if name.lower().startswith("qgc"):
            results[name] = "preserved-user-owned"
            continue
        pid = process.get("pid")
        expected = process.get("identity")
        actual = process_identity(pid) if isinstance(pid, int) and pid > 1 else None
        if actual is None:
            results[name] = "already-stopped"
        elif actual != expected:
            results[name] = "identity-mismatch-not-signalled"
        else:
            os.kill(pid, signal.SIGINT)
            deadline = time.monotonic() + STOP_TIMEOUT_SECONDS
            while time.monotonic() < deadline:
                current = process_identity(pid)
                if current is None:
                    results[name] = "stopped"
                    break
                if current != expected:
                    results[name] = "identity-changed-after-interrupt"
                    break
                time.sleep(STOP_POLL_SECONDS)
            else:
                results[name] = "interrupt-sent-stop-unconfirmed"
    complete = all(
        status in {"stopped", "already-stopped", "preserved-user-owned"}
        for status in results.values()
    )
    return {
        "runId": receipt.get("runId"),
        "status": "stopped" if complete else "partial",
        "results": results,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("status", "stop"))
    parser.add_argument("receipt", type=Path)
    args = parser.parse_args()
    try:
        result = inspect(args.receipt) if args.action == "status" else stop_owned(args.receipt)
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as error:
        print(f"profile {args.action} failed: {error}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
