#!/usr/bin/env python3
"""Capture bounded host load and process context for a swarm run."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import signal
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

STOP_REQUESTED = False


def request_stop(_signal: int, _frame: Any) -> None:
    global STOP_REQUESTED
    STOP_REQUESTED = True


def process_inventory() -> dict[str, Any]:
    result = subprocess.run(
        ["ps", "-Ao", "pid=,ppid=,%cpu=,%mem=,etime=,comm="],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return {"status": "unavailable", "reason": "ps-failed"}
    processes = []
    for line in result.stdout.splitlines():
        fields = line.strip().split(None, 5)
        if len(fields) != 6:
            continue
        try:
            process = {
                "pid": int(fields[0]),
                "parentPid": int(fields[1]),
                "cpuPercent": float(fields[2]),
                "memoryPercent": float(fields[3]),
                "elapsed": fields[4],
                "name": Path(fields[5].split(None, 1)[0]).name,
            }
        except (ValueError, OSError):
            continue
        processes.append(process)
    processes.sort(key=lambda item: (-item["cpuPercent"], item["pid"]))
    relevant = [
        item
        for item in processes
        if any(
            marker in item["name"].lower()
            for marker in (
                "gz",
                "gazebo",
                "nats",
                "cargo",
                "rustc",
                "clang",
                "cmake",
                "ninja",
                "make",
                "python",
                "node",
                "antoine",
                "qgroundcontrol",
            )
        )
    ]
    return {
        "status": "captured",
        "processCount": len(processes),
        "topCpuProcesses": processes[:30],
        "simulationAndBuildProcesses": relevant,
    }


def memory_snapshot() -> dict[str, Any]:
    if platform.system() != "Darwin":
        return {"status": "unavailable", "reason": "unsupported-platform"}
    result = subprocess.run(["vm_stat"], check=False, capture_output=True, text=True)
    if result.returncode != 0:
        return {"status": "unavailable", "reason": "vm-stat-failed"}
    return {"status": "captured", "vmStat": result.stdout.strip().splitlines()}


def capture_context(
    run_id: str, phase: str, project_root: Path, sample_sequence: int | None = None
) -> dict[str, Any]:
    disk = shutil.disk_usage(project_root)
    host_id = hashlib.sha256(
        f"{platform.node()}|{platform.platform()}|{platform.machine()}".encode()
    ).hexdigest()
    try:
        load_average = list(os.getloadavg())
        load_status = "captured"
    except (AttributeError, OSError):
        load_average = None
        load_status = "unavailable"
    context = {
        "contract": "damn-vulnerable-drone.swarm-host-context",
        "version": "1.0.0",
        "runId": run_id,
        "phase": phase,
        "capturedAtUtc": datetime.now(UTC).isoformat(),
        "capturedAtUnixNs": time.time_ns(),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "hostId": host_id,
        "logicalCpuCount": os.cpu_count(),
        "loadAverage": {"status": load_status, "values": load_average},
        "disk": {
            "path": str(project_root),
            "totalBytes": disk.total,
            "usedBytes": disk.used,
            "freeBytes": disk.free,
        },
        "memory": memory_snapshot(),
        "processes": process_inventory(),
    }
    if sample_sequence is not None:
        context["sampleSequence"] = sample_sequence
    return context


def capture_samples(
    run_id: str, project_root: Path, output: Path, stop_file: Path, interval_seconds: float
) -> int:
    global STOP_REQUESTED
    STOP_REQUESTED = False
    signal.signal(signal.SIGTERM, request_stop)
    sequence = 0
    with output.open("w", encoding="utf-8") as stream:
        while not STOP_REQUESTED and not stop_file.exists():
            sample = capture_context(run_id, "during", project_root, sequence)
            stream.write(json.dumps(sample, sort_keys=True) + "\n")
            stream.flush()
            sequence += 1
            deadline = time.monotonic() + interval_seconds
            while not STOP_REQUESTED and not stop_file.exists():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                time.sleep(min(0.25, remaining))
    return sequence


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--phase", choices=("pre", "during", "post"), required=True)
    parser.add_argument("--project-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--interval-seconds", type=float)
    parser.add_argument("--stop-file", type=Path)
    args = parser.parse_args()
    if not args.project_root.is_dir():
        parser.error("project root must be an existing directory")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    if args.interval_seconds is not None:
        if args.phase != "during" or args.stop_file is None or not 1 <= args.interval_seconds <= 60:
            parser.error(
                "sampling requires phase during, a stop file, and an interval of 1..60 seconds"
            )
        capture_samples(
            args.run_id,
            args.project_root.resolve(),
            args.output,
            args.stop_file,
            args.interval_seconds,
        )
    else:
        if args.stop_file is not None or args.phase == "during":
            parser.error("during phase requires interval sampling and a stop file")
        snapshot = capture_context(args.run_id, args.phase, args.project_root.resolve())
        args.output.write_text(
            json.dumps(snapshot, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
