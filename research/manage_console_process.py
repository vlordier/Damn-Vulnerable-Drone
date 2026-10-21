#!/usr/bin/env python3
"""Start and stop the research console as an owned detached local process."""

from __future__ import annotations

import os
import secrets
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / ".research-ui-runtime"
ACTIVE = RUNTIME / "active"
PID_FILE = ACTIVE / "pid"
IDENTITY_FILE = ACTIVE / "supervisor.identity"
LOG_FILE = RUNTIME / "console.log"
SERVER_MODULE = "simulator.mgmt.research_console"


def safe_paths() -> None:
    if RUNTIME.is_symlink() or (RUNTIME.exists() and not RUNTIME.is_dir()):
        raise SystemExit(f"Refusing unsafe research UI runtime path: {RUNTIME}")
    if ACTIVE.is_symlink():
        raise SystemExit("Refusing symlinked research UI active marker.")
    if ACTIVE.exists() and not ACTIVE.is_dir():
        raise SystemExit("Refusing non-directory research UI active marker.")
    for path in (PID_FILE, IDENTITY_FILE, LOG_FILE):
        if path.is_symlink() or (path.exists() and not path.is_file()):
            raise SystemExit(f"Refusing unsafe research UI runtime file: {path}")


def pid_from_marker() -> int | None:
    try:
        value = PID_FILE.read_text(encoding="utf-8").strip()
        first_line = value.splitlines()[0] if value else ""
        return int(first_line) if first_line.isdigit() else None
    except OSError:
        return None


def process_command(pid: int) -> str:
    completed = subprocess.run(
        ["ps", "-ww", "-p", str(pid), "-o", "command="],
        check=False,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def supervisor_metadata() -> dict[str, str]:
    try:
        lines = IDENTITY_FILE.read_text(encoding="utf-8").splitlines()
    except OSError:
        return {}
    return dict(line.split("=", 1) for line in lines if "=" in line)


def print_running(pid: int) -> None:
    try:
        lines = PID_FILE.read_text(encoding="utf-8").splitlines()
        port = int(lines[1]) if len(lines) > 1 else 0
    except (OSError, ValueError):
        port = 0
    if not 1 <= port <= 65535:
        print(
            f"Research console is running (PID {pid}); its configured port could not be determined."
        )
        return
    print(
        f"Research console is running at http://127.0.0.1:{port}/attacks/research (supervisor PID {pid})."
    )


def marker_port() -> int | None:
    try:
        lines = PID_FILE.read_text(encoding="utf-8").splitlines()
        port = int(lines[1]) if len(lines) > 1 else 0
    except (OSError, ValueError):
        return None
    return port if 1 <= port <= 65535 else None


def owned_process(pid: int | None) -> bool:
    if pid is None:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    metadata = supervisor_metadata()
    supervisor = os.environ.get("DVD_PROCESS_SUPERVISOR", "")
    try:
        child_pid = int(metadata.get("child_pid", "0"))
    except ValueError:
        return False
    return (
        Path(supervisor).is_file()
        and metadata.get("pid") == str(pid)
        and str(IDENTITY_FILE) in process_command(pid)
        and Path(supervisor).name in process_command(pid)
        and SERVER_MODULE in process_command(child_pid)
    )


def process_alive(pid: int | None) -> bool:
    if pid is None:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def clear_marker() -> None:
    PID_FILE.unlink(missing_ok=True)
    IDENTITY_FILE.unlink(missing_ok=True)
    try:
        ACTIVE.rmdir()
    except OSError:
        pass


def start() -> int:
    RUNTIME.mkdir(parents=True, exist_ok=True)
    current = pid_from_marker()
    if owned_process(current):
        requested_port = os.environ.get("DVD_RESEARCH_UI_PORT", "8001")
        if requested_port.isdigit() and marker_port() != int(requested_port):
            print(
                f"Research console already runs on port {marker_port()}; stop it before changing to port {requested_port}.",
                file=sys.stderr,
            )
            return 1
        print_running(current)
        return 0
    if process_alive(current):
        print(
            f"Refusing to replace the active marker for unrelated PID {current}.", file=sys.stderr
        )
        return 1
    clear_marker()
    ACTIVE.mkdir()
    env = os.environ.copy()
    port = env.get("DVD_RESEARCH_UI_PORT", "8001")
    if not port.isdigit() or not 1 <= int(port) <= 65535:
        ACTIVE.rmdir()
        raise SystemExit("DVD_RESEARCH_UI_PORT must be an integer from 1 to 65535")
    env["RESEARCH_DIR"] = str(ROOT / "research")
    env["RESEARCH_EVIDENCE_DIR"] = str(ROOT / "build_logs" / "research" / "antoine-scenarios")
    log = LOG_FILE.open("ab", buffering=0)
    owner_token = secrets.token_hex(16)
    supervisor = os.environ.get("DVD_PROCESS_SUPERVISOR", "")
    if not supervisor or not Path(supervisor).is_file():
        ACTIVE.rmdir()
        raise SystemExit(
            "DVD_PROCESS_SUPERVISOR is unavailable; refusing an unsupervised console start"
        )
    process = subprocess.Popen(
        [
            supervisor,
            "run",
            str(IDENTITY_FILE),
            "--",
            sys.executable,
            "-m",
            SERVER_MODULE,
            "--dvd-owner-token",
            owner_token,
        ],
        cwd=ROOT,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
        close_fds=True,
    )
    log.close()
    PID_FILE.write_text(f"{process.pid}\n{port}\n", encoding="utf-8")
    for _ in range(40):
        if IDENTITY_FILE.is_file():
            break
        if process.poll() is not None:
            clear_marker()
            print(
                f"Research console supervisor exited with status {process.returncode}.",
                file=sys.stderr,
            )
            return 1
        time.sleep(0.025)
    if not IDENTITY_FILE.is_file():
        print("Research console supervisor did not publish its ownership record.", file=sys.stderr)
        return 1
    print(process.pid)
    return 0


def status() -> int:
    pid = pid_from_marker()
    if owned_process(pid):
        print_running(pid)
        return 0
    print("Research console is stopped.")
    return 1


def stop() -> int:
    pid = pid_from_marker()
    if pid is None:
        clear_marker()
        print("Research console is already stopped.")
        return 0
    if not process_alive(pid):
        clear_marker()
        print("Research console is already stopped.")
        return 0
    if not owned_process(pid):
        print(
            f"Refusing to stop PID {pid}: it is not this checkout's supervised research console.",
            file=sys.stderr,
        )
        return 1
    supervisor = os.environ.get("DVD_PROCESS_SUPERVISOR", "")
    stopped = subprocess.run([supervisor, "stop", str(IDENTITY_FILE)], check=False)
    if stopped.returncode != 0:
        print(f"Native supervisor refused to stop PID {pid}.", file=sys.stderr)
        return 1
    for _ in range(40):
        if not process_alive(pid) or str(IDENTITY_FILE) not in process_command(pid):
            clear_marker()
            print("Research console stopped.")
            return 0
        time.sleep(0.25)
    print(f"Research console has not exited; inspect {LOG_FILE}.", file=sys.stderr)
    return 1


def main() -> int:
    safe_paths()
    if len(sys.argv) != 2 or sys.argv[1] not in {"start", "status", "stop"}:
        print(f"Usage: {sys.argv[0]} {{start|status|stop}}", file=sys.stderr)
        return 2
    return {"start": start, "status": status, "stop": stop}[sys.argv[1]]()


if __name__ == "__main__":
    raise SystemExit(main())
