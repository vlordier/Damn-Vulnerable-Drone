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
import tempfile
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
    flight_directory = receipt.get("flightRunDirectory")
    if isinstance(flight_directory, str) and flight_directory:
        flight_path = Path(flight_directory).resolve()
    else:
        nested = run_path / "flight-proof"
        flight_path = (nested if nested.is_dir() else run_path).resolve()
    resolved_run = run_path.resolve()
    if flight_path != resolved_run and not flight_path.is_relative_to(resolved_run):
        return {
            "status": "invalid",
            "reason": "flight evidence directory is outside the run directory",
        }
    nats_log = flight_path / "logs" / "nats.log"
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

    messages, error = read_nats_messages(port, f"furia.telemetry.{callsign}")
    if error:
        return error
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
    times = [message.get("time_s") for message in messages[-2:]]
    if all(
        isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
        for value in times
    ):
        first_time, latest_time = times
        timestamp_subject = f"furia.telemetry.{callsign}"
        timestamp_unit = 1.0
    else:
        states, error = read_nats_messages(port, f"furia.gzsitl.state.{callsign}")
        if error:
            return error
        if len(states) < 2 or not all(isinstance(state, dict) for state in states):
            return {"status": "invalid", "reason": "NATS did not return two Gazebo state samples"}
        for state in states[-2:]:
            if state.get("schema") != "furia.gzsitl.state" or state.get("identity") != callsign:
                return {
                    "status": "invalid",
                    "reason": "Gazebo state schema or callsign does not match this profile",
                }
            simulation_time_ns = state.get("simulation_time_ns")
            if (
                not isinstance(simulation_time_ns, (int, float))
                or isinstance(simulation_time_ns, bool)
                or not math.isfinite(simulation_time_ns)
            ):
                return {
                    "status": "invalid",
                    "reason": "Gazebo state has no finite simulation timestamp",
                }
        first_time, latest_time = (state["simulation_time_ns"] for state in states[-2:])
        timestamp_subject = f"furia.gzsitl.state.{callsign}"
        timestamp_unit = 1_000_000_000.0
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
        "timestampSubject": timestamp_subject,
        "simulationTimeSeconds": latest_time / timestamp_unit,
        "simulationTimeAdvanceSeconds": (latest_time - first_time) / timestamp_unit,
        "sampleCount": len(messages),
        "armed": message.get("armed") if isinstance(message.get("armed"), bool) else None,
        "mode": message.get("mode"),
    }


def read_nats_messages(port: int, subject: str) -> tuple[list[Any], dict[str, str] | None]:
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
                subject,
            ],
            capture_output=True,
            text=True,
            timeout=TELEMETRY_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return [], {
            "status": "unavailable",
            "reason": "no fresh telemetry arrived before the bounded deadline",
        }
    if result.returncode != 0:
        return [], {"status": "unavailable", "reason": f"NATS subscription failed for {subject}"}
    try:
        return [json.loads(line) for line in result.stdout.splitlines() if line.strip()], None
    except (json.JSONDecodeError, TypeError):
        return [], {"status": "invalid", "reason": f"NATS returned non-JSON data for {subject}"}


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
    owned_processes = [
        item
        for name, item in checks.items()
        if isinstance(item, dict) and "pid" in item and not name.lower().startswith("qgc")
    ]
    run_owned_ports = [checks.get("natsPort"), checks.get("bridgeUdpBindPort")]
    stopped = (
        bool(owned_processes)
        and all(not item["alive"] for item in owned_processes)
        and all(
            isinstance(item, dict) and item.get("listening") is False for item in run_owned_ports
        )
        and not foreign
    )
    if stopped:
        overall_status = "stopped"
    elif telemetry.get("status") != "fresh":
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
    run_id = receipt.get("runId")
    run_directory = receipt.get("runDirectory")
    if (
        not isinstance(run_id, str)
        or not run_id
        or not isinstance(run_directory, str)
        or Path(run_directory).name != run_id
        or not Path(run_directory).is_dir()
    ):
        return _persist_stop_result(
            receipt_path,
            {
                "runId": run_id,
                "status": "partial",
                "results": {"runReceipt": "invalid-run-identity-not-signalled"},
            },
        )

    processes = receipt.get("processes")
    if not isinstance(processes, dict):
        return _persist_stop_result(
            receipt_path,
            {
                "runId": run_id,
                "status": "partial",
                "results": {"processes": "invalid-process-records-not-signalled"},
            },
        )
    if not any(not name.lower().startswith("qgc") for name in processes):
        return _persist_stop_result(
            receipt_path,
            {
                "runId": run_id,
                "status": "partial",
                "results": {"processes": "no-run-owned-processes-recorded"},
            },
        )

    results = {}
    for name, process in processes.items():
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
            stop_result = _wait_for_owned_exit(pid, expected)
            if stop_result == "identity-changed-after-interrupt":
                # Never signal the changed identity. Give the original
                # interrupt a bounded chance to finish its exec handoff.
                stop_result = _wait_for_pid_exit(pid)
            if stop_result == "timeout":
                # Background shells can inherit SIGINT as ignored. Recheck
                # identity before TERM so a reused PID is never signalled.
                current = process_identity(pid)
                if current is None:
                    stop_result = "stopped"
                elif current != expected:
                    stop_result = "identity-changed-after-interrupt"
                else:
                    os.kill(pid, signal.SIGTERM)
                    stop_result = _wait_for_owned_exit(pid, expected)
                    if stop_result == "timeout":
                        stop_result = "term-sent-stop-unconfirmed"
            results[name] = stop_result
    complete = all(
        status
        in {
            "stopped",
            "stopped-after-identity-change",
            "already-stopped",
            "preserved-user-owned",
        }
        for status in results.values()
    )
    return _persist_stop_result(
        receipt_path,
        {
            "runId": receipt.get("runId"),
            "status": "stopped" if complete else "partial",
            "results": results,
        },
    )


def _persist_stop_result(receipt_path: Path, result: dict[str, Any]) -> dict[str, Any]:
    """Write a compact stop report beside the receipt without changing it."""
    report = {**result, "observedAtUtc": datetime.now(UTC).isoformat()}
    report_path = receipt_path.with_name("interactive-profile-stop-result.json")
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=receipt_path.parent,
            prefix=".interactive-profile-stop-",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            json.dump(report, temporary, indent=2, sort_keys=True)
            temporary.write("\n")
        os.replace(temporary_path, report_path)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
    return report


def _wait_for_owned_exit(pid: int, expected: str) -> str:
    deadline = time.monotonic() + STOP_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        current = process_identity(pid)
        if current is None:
            return "stopped"
        if current != expected:
            return "identity-changed-after-interrupt"
        time.sleep(STOP_POLL_SECONDS)
    return "timeout"


def _wait_for_pid_exit(pid: int) -> str:
    deadline = time.monotonic() + STOP_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if process_identity(pid) is None:
            return "stopped-after-identity-change"
        time.sleep(STOP_POLL_SECONDS)
    return "identity-changed-after-interrupt"


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
    return 1 if args.action == "stop" and result.get("status") != "stopped" else 0


if __name__ == "__main__":
    raise SystemExit(main())
