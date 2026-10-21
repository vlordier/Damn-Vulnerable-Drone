#!/usr/bin/env python3
"""Inspect and safely stop a QGC/Gazebo run using its run-owned receipt."""

from __future__ import annotations

import argparse
import json
import math
import os
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
PROFILE_CONTRACT = "damn-vulnerable-drone.interactive-profile"
PROFILE_VERSION = "1.0.0"


def _load_receipt(receipt_path: Path) -> dict[str, Any]:
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    if not isinstance(receipt, dict):
        raise ValueError("profile receipt must be a JSON object")
    if (
        receipt.get("contract") != PROFILE_CONTRACT
        or receipt.get("version") != PROFILE_VERSION
        or receipt.get("profile") != "interactive-antoine-gazebo"
    ):
        raise ValueError("unsupported interactive profile receipt contract or version")
    return receipt


def _run_directory_error(receipt_path: Path, receipt: dict[str, Any]) -> str | None:
    run_id = receipt.get("runId")
    run_directory = receipt.get("runDirectory")
    if not isinstance(run_id, str) or not run_id or Path(run_id).name != run_id:
        return "runId must be a single non-empty directory name"
    if not isinstance(run_directory, str) or not Path(run_directory).is_absolute():
        return "runDirectory must be an absolute path"
    run_path = Path(run_directory)
    try:
        resolved_run = run_path.resolve(strict=True)
        resolved_receipt_parent = receipt_path.resolve(strict=True).parent
    except (OSError, RuntimeError):
        return "run directory or receipt path cannot be resolved"
    if not run_path.is_dir() or run_path.name != run_id:
        return "run directory is missing or does not match runId"
    if resolved_run != resolved_receipt_parent:
        return "receipt is not located inside its declared run directory"
    return None


def process_identity(pid: int) -> str | None:
    result = subprocess.run(
        ["ps", "-p", str(pid), "-o", "lstart=", "-o", "command="],
        capture_output=True,
        text=True,
        check=False,
    )
    value = result.stdout.strip()
    if result.returncode > 1:
        detail = result.stderr.strip() or f"ps exited with status {result.returncode}"
        raise OSError(f"cannot inspect process {pid}: {detail}")
    return value if result.returncode == 0 and value else None


def _callsign_error(callsign: Any) -> str | None:
    if not isinstance(callsign, str) or not callsign:
        return "callsign must be a non-empty string"
    if any(
        ord(character) < 33 or ord(character) == 127 or character in ".*>" for character in callsign
    ):
        return "callsign must be a single NATS subject token"
    return None


def sample_telemetry(receipt: dict[str, Any]) -> dict[str, Any]:
    """Read one newly published state from this run's loopback NATS broker."""
    callsign = receipt.get("callsign")
    ports = receipt.get("ports")
    port = ports.get("nats") if isinstance(ports, dict) else None
    run_directory = receipt.get("runDirectory")
    run_id = receipt.get("runId")
    callsign_error = _callsign_error(callsign)
    if (
        callsign_error
        or not isinstance(port, int)
        or isinstance(port, bool)
        or not 1 <= port <= 65535
        or not isinstance(run_directory, str)
        or not isinstance(run_id, str)
    ):
        return {
            "status": "unavailable",
            "reason": callsign_error or "receipt lacks valid run or NATS port",
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
    receipt = _load_receipt(receipt_path)
    run_directory_error = _run_directory_error(receipt_path, receipt)
    if run_directory_error:
        return {
            "contract": "damn-vulnerable-drone.interactive-profile-status",
            "version": "1.0.0",
            "observedAtUtc": datetime.now(UTC).isoformat(),
            "runId": receipt.get("runId"),
            "runDirectory": receipt.get("runDirectory"),
            "callsign": receipt.get("callsign"),
            "ports": receipt.get("ports"),
            "runIdentityValidation": {"status": "invalid", "reason": run_directory_error},
            "portValidation": {"status": "not-checked", "errors": []},
            "processValidation": {
                "status": "not-checked",
                "reason": "run identity failed; process probes were skipped",
            },
            "processes": {},
            "status": "partial",
            "telemetry": {"status": "unavailable", "reason": "run identity failed"},
            "qgcPeerRouting": {
                "status": "unverified",
                "reason": "run identity failed; routing was not inspected",
            },
            "armed": None,
            "armedReason": "run identity failed; armed state is unknown",
        }
    callsign_error = _callsign_error(receipt.get("callsign"))
    if callsign_error:
        return {
            "contract": "damn-vulnerable-drone.interactive-profile-status",
            "version": "1.0.0",
            "observedAtUtc": datetime.now(UTC).isoformat(),
            "runId": receipt.get("runId"),
            "runDirectory": receipt.get("runDirectory"),
            "callsign": receipt.get("callsign"),
            "ports": receipt.get("ports"),
            "runIdentityValidation": {"status": "valid", "reason": None},
            "portValidation": {"status": "not-checked", "errors": []},
            "processValidation": {
                "status": "not-checked",
                "reason": "callsign is invalid; process probes were skipped",
            },
            "processes": {},
            "status": "partial",
            "telemetry": {"status": "invalid", "reason": callsign_error},
            "qgcPeerRouting": {
                "status": "unverified",
                "reason": "callsign is invalid; routing was not inspected",
            },
            "armed": None,
            "armedReason": "callsign is invalid; armed state is unknown",
        }
    checks: dict[str, Any] = {}
    processes = receipt.get("processes")
    process_records_valid = isinstance(processes, dict)
    process_error = None if process_records_valid else "processes must be an object"
    if not process_records_valid:
        processes = {}
    for name, process in processes.items():
        if not isinstance(process, dict):
            process_records_valid = False
            process_error = "each process record must be an object"
            checks[name] = {
                "pid": None,
                "alive": False,
                "identityMatches": False,
                "error": "invalid-process-record",
            }
            continue
        control_identity = process.get("supervisorIdentityPath")
        if control_identity is not None and (
            not isinstance(control_identity, str) or not control_identity
        ):
            process_records_valid = False
            process_error = "supervisor identity path must be a non-empty string"
        elif control_identity is not None and _supervisor_identity_path(receipt, process) is None:
            process_records_valid = False
            process_error = "supervisor identity must be a regular run-local file"
        pid = process.get("pid")
        try:
            actual = process_identity(pid) if isinstance(pid, int) and pid > 1 else None
        except OSError:
            checks[name] = {
                "pid": pid,
                "alive": None,
                "identityMatches": False,
                "error": "identity-probe-unavailable",
            }
            continue
        checks[name] = {
            "pid": pid,
            "alive": actual is not None,
            "identityMatches": actual == process.get("identity") if actual else False,
        }
    ports = receipt.get("ports")
    ports_valid, port_errors = _validate_profile_ports(ports)
    if not isinstance(ports, dict):
        ports = {}
    for name, protocol in (("nats", "TCP"), ("qgcUdpListen", "UDP"), ("bridgeUdpBind", "UDP")):
        port = ports.get(name)
        port_valid = isinstance(port, int) and not isinstance(port, bool) and 1 <= port <= 65535
        if not port_valid:
            checks[f"{name}Port"] = {
                "port": port,
                "protocol": protocol,
                "listening": None,
                "error": "invalid-port-not-checked",
            }
            continue
        selector = f"-i{protocol}:{port}"
        command = ["lsof", "-nP", selector]
        if protocol == "TCP":
            command.extend(["-sTCP:LISTEN"])
        try:
            probe = subprocess.run(command, capture_output=True, text=True, check=False)
        except OSError:
            checks[f"{name}Port"] = {
                "port": port,
                "protocol": protocol,
                "listening": None,
                "error": "port-probe-unavailable",
            }
            continue
        checks[f"{name}Port"] = {
            "port": port,
            "protocol": protocol,
            "listening": True
            if probe.returncode == 0
            else False
            if probe.returncode == 1
            else None,
            **({"error": "port-probe-failed"} if probe.returncode > 1 else {}),
        }
    stale = _run_directory_error(receipt_path, receipt) is not None
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
    required_process_names = ("antoineGazeboSupervisor", "mavlinkBridge")
    missing_required_processes = [
        name
        for name in required_process_names
        if name not in processes or not isinstance(processes.get(name), dict)
    ]
    required_processes_matched = not missing_required_processes and all(
        checks[name].get("alive") is True and checks[name].get("identityMatches") is True
        for name in required_process_names
    )
    telemetry = sample_telemetry(receipt)
    bridge = checks.get("mavlinkBridge", {})
    bridge_process = processes.get("mavlinkBridge", {})
    bridge_identity = bridge_process.get("identity", "") if isinstance(bridge_process, dict) else ""
    expected_qgc_peer = f"127.0.0.1:{ports.get('qgcUdpListen')}"
    expected_bridge_bind = f"127.0.0.1:{ports.get('bridgeUdpBind')}"
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
    active_ports = [
        checks.get("natsPort"),
        checks.get("qgcUdpListenPort"),
        checks.get("bridgeUdpBindPort"),
    ]
    stopped = (
        process_records_valid
        and ports_valid
        and not missing_required_processes
        and bool(owned_processes)
        and all(not item["alive"] for item in owned_processes)
        and all(
            isinstance(item, dict) and item.get("listening") is False for item in run_owned_ports
        )
        and not foreign
    )
    if stopped:
        overall_status = "stopped"
    elif not process_records_valid:
        overall_status = "partial"
    elif not required_processes_matched:
        overall_status = "partial"
    elif not ports_valid:
        overall_status = "partial"
    elif not all(isinstance(item, dict) and item.get("listening") is True for item in active_ports):
        overall_status = "partial"
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
        "runIdentityValidation": {
            "status": "stale" if stale else "valid",
            "reason": "run directory changed during inspection" if stale else None,
        },
        "callsign": receipt.get("callsign"),
        "ports": receipt.get("ports"),
        "portValidation": {"status": "valid" if ports_valid else "invalid", "errors": port_errors},
        "receiptStatus": receipt.get("status"),
        "processes": checks,
        "processValidation": {
            "status": (
                "invalid"
                if not process_records_valid
                else "valid"
                if required_processes_matched
                else "incomplete"
            ),
            "reason": process_error
            or (
                None
                if required_processes_matched
                else "required supervisor and bridge identities must match"
            ),
        },
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
    receipt = _load_receipt(receipt_path)
    run_id = receipt.get("runId")
    run_directory_error = _run_directory_error(receipt_path, receipt)
    if run_directory_error:
        return _persist_stop_result(
            receipt_path,
            {
                "runId": run_id,
                "status": "partial",
                "results": {"runReceipt": run_directory_error},
            },
        )
    receipt_path = receipt_path.resolve(strict=True)

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

    required_process_names = ("antoineGazeboSupervisor", "mavlinkBridge")
    missing_required_processes = [
        name
        for name in required_process_names
        if name not in processes or not isinstance(processes.get(name), dict)
    ]
    results = {}
    for name, process in processes.items():
        if name.lower().startswith("qgc"):
            results[name] = "preserved-user-owned"
            continue
        if not isinstance(process, dict):
            results[name] = "invalid-process-identity-not-signalled"
            continue
        pid = process.get("pid")
        expected = process.get("identity")
        if (
            not isinstance(pid, int)
            or isinstance(pid, bool)
            or pid <= 1
            or not isinstance(expected, str)
            or not expected
        ):
            results[name] = "invalid-process-identity-not-signalled"
            continue
        try:
            actual = process_identity(pid)
        except OSError:
            results[name] = "identity-probe-unavailable"
            continue
        if actual is None:
            results[name] = "already-stopped"
        elif actual != expected:
            results[name] = "identity-mismatch-not-signalled"
        else:
            control_path = _supervisor_identity_path(receipt, process)
            if control_path is None:
                results[name] = "supervisor-identity-required-not-signalled"
            else:
                try:
                    results[name] = _stop_supervised_process(pid, expected, control_path)
                except OSError:
                    results[name] = "identity-probe-unavailable"
    if missing_required_processes:
        results["requiredProcessRecords"] = "missing:" + ",".join(missing_required_processes)
    results.update(_run_owned_port_statuses(receipt))
    complete = all(
        status
        in {
            "stopped",
            "already-stopped",
            "preserved-user-owned",
            "closed",
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


def _validate_profile_ports(ports: Any) -> tuple[bool, list[str]]:
    if not isinstance(ports, dict):
        return False, ["ports must be an object"]
    errors = []
    for name in ("nats", "qgcUdpListen", "bridgeUdpBind"):
        port = ports.get(name)
        if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
            errors.append(f"{name} must be an integer port from 1 to 65535")
    qgc_port = ports.get("qgcUdpListen")
    bridge_port = ports.get("bridgeUdpBind")
    if (
        isinstance(qgc_port, int)
        and not isinstance(qgc_port, bool)
        and isinstance(bridge_port, int)
        and not isinstance(bridge_port, bool)
        and qgc_port == bridge_port
    ):
        errors.append("qgcUdpListen and bridgeUdpBind must use different UDP ports")
    return not errors, errors


def _run_owned_port_statuses(receipt: dict[str, Any]) -> dict[str, str]:
    ports = receipt.get("ports")
    checks = {}
    for name, protocol in (("nats", "TCP"), ("bridgeUdpBind", "UDP")):
        port = ports.get(name) if isinstance(ports, dict) else None
        result_name = f"{name}Port"
        if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
            checks[result_name] = "invalid-port-not-verified"
            continue
        command = ["lsof", "-nP", f"-i{protocol}:{port}"]
        if protocol == "TCP":
            command.extend(["-sTCP:LISTEN"])
        try:
            probe = subprocess.run(command, capture_output=True, text=True, check=False)
        except OSError:
            checks[result_name] = "unavailable"
            continue
        checks[result_name] = (
            "open"
            if probe.returncode == 0
            else "closed"
            if probe.returncode == 1
            else "unavailable"
        )
    return checks


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


def _supervisor_identity_path(receipt: dict[str, Any], process: dict[str, Any]) -> Path | None:
    value = process.get("supervisorIdentityPath")
    if not isinstance(value, str) or not value:
        return None
    candidate = Path(value)
    run_directory = receipt.get("runDirectory")
    if not candidate.is_absolute() or candidate.is_symlink() or not isinstance(run_directory, str):
        return None
    try:
        run_path = Path(run_directory).resolve(strict=True)
        resolved = candidate.resolve(strict=True)
        if resolved.parent != run_path or not resolved.is_file():
            return None
    except (OSError, RuntimeError):
        return None
    return resolved


def _stop_supervised_process(pid: int, expected: str, identity_path: Path) -> str:
    """Request stop through the run-owned supervisor FIFO, never by PID."""
    supervisor = Path(__file__).resolve().parent / ".process-supervisor"
    if supervisor.is_symlink() or not supervisor.is_file() or not os.access(supervisor, os.X_OK):
        return "process-supervisor-unavailable"
    try:
        request = subprocess.run(
            [str(supervisor), "stop", str(identity_path)],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return "process-supervisor-unavailable"
    if request.returncode != 0:
        try:
            current = process_identity(pid)
        except OSError:
            return "identity-probe-unavailable"
        return "already-stopped" if current is None else "supervisor-stop-rejected"
    return _wait_for_owned_exit(pid, expected)


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
