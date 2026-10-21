#!/usr/bin/env python3
"""Record a verified process identity in an interactive profile receipt."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import tempfile
import time
from pathlib import Path


def record_process(
    receipt_path: Path,
    name: str,
    pid: int,
    *,
    status: str | None = None,
    run_directory: str | None = None,
    supervisor_identity_path: Path | None = None,
) -> None:
    if not name.strip():
        raise ValueError("process name must not be empty")
    if pid <= 1:
        raise ValueError("process PID must be greater than 1")

    identity = ""
    detail = "process identity was not observable"
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "lstart=", "-o", "command="],
            capture_output=True,
            text=True,
            check=False,
        )
        identity = result.stdout.strip()
        if result.returncode == 0 and identity:
            break
        detail = result.stderr.strip() or f"ps exited with status {result.returncode}"
        time.sleep(0.025)
    else:
        raise ValueError(f"cannot record process identity for {name}: {detail}")

    receipt = json.loads(receipt_path.read_text())
    if not isinstance(receipt, dict) or not isinstance(receipt.get("processes"), dict):
        raise ValueError("interactive profile receipt must contain a processes object")
    process_record = {"pid": pid, "identity": identity}
    if supervisor_identity_path is not None:
        run_directory = receipt.get("runDirectory")
        if not isinstance(run_directory, str):
            raise ValueError(
                "receipt must declare its run directory before recording supervisor identity"
            )
        if supervisor_identity_path.is_symlink():
            raise ValueError("refusing a symlink as the supervisor identity")
        resolved_run = Path(run_directory).resolve(strict=True)
        resolved_identity = supervisor_identity_path.resolve(strict=True)
        if resolved_identity.parent != resolved_run or not resolved_identity.is_file():
            raise ValueError("supervisor identity must be a regular file inside the run directory")
        process_record["supervisorIdentityPath"] = str(resolved_identity)
    receipt["processes"][name] = process_record
    if status is not None:
        receipt["status"] = status
    if run_directory is not None:
        receipt["runDirectory"] = str(Path(run_directory).resolve())

    fd, temporary_name = tempfile.mkstemp(prefix=f".{receipt_path.name}.", dir=receipt_path.parent)
    try:
        with os.fdopen(fd, "w") as temporary:
            json.dump(receipt, temporary, indent=2, sort_keys=True)
            temporary.write("\n")
        os.replace(temporary_name, receipt_path)
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("receipt", type=Path)
    parser.add_argument("name")
    parser.add_argument("pid", type=int)
    parser.add_argument("--status")
    parser.add_argument("--run-directory")
    parser.add_argument("--supervisor-identity", type=Path)
    args = parser.parse_args()
    try:
        record_process(
            args.receipt,
            args.name,
            args.pid,
            status=args.status,
            run_directory=args.run_directory,
            supervisor_identity_path=args.supervisor_identity,
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
