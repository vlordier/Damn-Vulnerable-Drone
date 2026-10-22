"""Fail-closed process identity helpers for run-owned local processes."""

from __future__ import annotations

import argparse
import json
import os
import platform
import signal
import subprocess
import sys
from pathlib import Path

from dvd_research.strict_json import loads as strict_json_loads


def inspect(pid: int) -> dict[str, str] | None:
    if pid <= 1:
        return None
    values: dict[str, str] = {}
    for field in ("lstart", "command", "pgid"):
        result = subprocess.run(
            ["ps", "-ww", "-p", str(pid), "-o", f"{field}="],
            capture_output=True,
            text=True,
            check=False,
        )
        value = result.stdout.strip()
        if result.returncode != 0 or not value:
            return None
        values[field] = value
    return values


def capture(pid: int) -> dict[str, str]:
    identity = inspect(pid)
    if identity is None:
        raise ValueError(f"process {pid} is not live")
    identity["pid"] = str(pid)
    return identity


def matches(path: Path, pid: int) -> bool:
    try:
        expected = strict_json_loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    actual = inspect(pid)
    return (
        isinstance(expected, dict)
        and actual is not None
        and all(expected.get(key) == actual.get(key) for key in ("lstart", "command", "pgid"))
        and expected.get("pid") == str(pid)
    )


def matches_owned_child(path: Path, pid: int) -> bool:
    """Match a child across exec while rejecting PID reuse and PGID changes."""
    try:
        expected = strict_json_loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    actual = inspect(pid)
    return (
        isinstance(expected, dict)
        and actual is not None
        and expected.get("pid") == str(pid)
        and expected.get("lstart") == actual.get("lstart")
        and expected.get("pgid") == actual.get("pgid")
    )


def record(pid: int, path: Path) -> None:
    identity = capture(pid)
    if path.is_symlink():
        raise ValueError("refusing a symlink as the process identity record")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(identity, sort_keys=True) + "\n", encoding="utf-8")
    path.chmod(0o600)


def signal_owned_child(pid: int, path: Path, signal_number: int) -> None:
    if not matches_owned_child(path, pid):
        raise ValueError("refusing to signal a process whose PID/start/PGID identity differs")
    pidfd_open = getattr(os, "pidfd_open", None)
    pidfd_send_signal = getattr(signal, "pidfd_send_signal", None)
    if platform.system() == "Linux" and pidfd_open and pidfd_send_signal:
        pidfd = pidfd_open(pid, 0)
        try:
            if not matches_owned_child(path, pid):
                raise ValueError("process identity changed while opening its pidfd")
            pidfd_send_signal(pidfd, signal_number)
        finally:
            os.close(pidfd)
        return
    raise ValueError(
        "atomic child signalling is unavailable; use the native parent-owned supervisor"
    )


def signal_owned_process(pid: int, path: Path, signal_number: int) -> None:
    """Signal a recorded process using a stable kernel handle when available."""
    if not matches(path, pid):
        raise ValueError(
            "refusing to signal a process whose PID/start/command/PGID identity differs"
        )
    pidfd_open = getattr(os, "pidfd_open", None)
    pidfd_send_signal = getattr(signal, "pidfd_send_signal", None)
    if platform.system() == "Linux" and pidfd_open and pidfd_send_signal:
        pidfd = pidfd_open(pid, 0)
        try:
            if not matches(path, pid):
                raise ValueError("process identity changed while opening its pidfd")
            pidfd_send_signal(pidfd, signal_number)
        finally:
            os.close(pidfd)
        return
    raise ValueError(
        "atomic process signalling is unavailable; use the native parent-owned supervisor"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    capture_parser = commands.add_parser("capture")
    capture_parser.add_argument("--pid", required=True, type=int)
    capture_parser.add_argument("--identity", required=True, type=Path)
    signal_parser = commands.add_parser("signal")
    signal_parser.add_argument("--pid", required=True, type=int)
    signal_parser.add_argument("--identity", required=True, type=Path)
    signal_parser.add_argument("--signal", choices=("TERM", "INT"), default="TERM")
    signal_parser.add_argument("--process", action="store_true")
    args = parser.parse_args()
    try:
        if args.command == "capture":
            record(args.pid, args.identity)
        else:
            number = signal.SIGTERM if args.signal == "TERM" else signal.SIGINT
            if args.process:
                signal_owned_process(args.pid, args.identity, number)
            else:
                signal_owned_child(args.pid, args.identity, number)
    except (OSError, ValueError) as error:
        print(f"process identity guard: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
