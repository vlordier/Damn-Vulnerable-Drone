#!/usr/bin/env python3
"""Create a reproducible source/build receipt for the interactive QGC bench."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def sha256(path: Path) -> str | None:
    if path.is_symlink() or not path.is_file():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True, check=False
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def source_record(name: str, root: Path) -> dict[str, Any]:
    revision = git(root, "rev-parse", "HEAD") or None
    if revision is None:
        return {
            "name": name,
            "revision": None,
            "classification": "unavailable",
            "reason": "source path is missing or is not a Git checkout",
        }

    status = subprocess.run(
        ["git", "-C", str(root), "status", "--porcelain=v1", "-z", "--untracked-files=all"],
        capture_output=True,
        check=False,
    )
    diff = subprocess.run(
        ["git", "-C", str(root), "diff", "--binary", "HEAD", "--"],
        capture_output=True,
        check=False,
    )
    if status.returncode or diff.returncode:
        return {
            "name": name,
            "revision": revision,
            "classification": "unavailable",
            "reason": "Git could not inspect source worktree state",
        }

    entries = status.stdout.split(b"\0")
    dirty_files: list[dict[str, str]] = []
    for entry in entries:
        if len(entry) < 4 or entry[2:3] != b" ":
            continue
        # Porcelain -z places the two status columns before the path. Paths are
        # decoded with surrogateescape so unusual filenames remain hashable.
        state, raw_path = entry[:2], entry[3:]
        path = raw_path.decode("utf-8", errors="surrogateescape")
        file_path = root / path
        if file_path.is_file() and not file_path.is_symlink():
            content_hash = sha256(file_path)
        elif file_path.is_symlink():
            content_hash = hashlib.sha256(str(file_path.readlink()).encode()).hexdigest()
        else:
            content_hash = hashlib.sha256(b"<deleted-or-non-file>").hexdigest()
        dirty_files.append(
            {
                "path": path,
                "state": state.decode("ascii", errors="replace"),
                "sha256": content_hash or "",
            }
        )

    dirty_payload = json.dumps(dirty_files, sort_keys=True, separators=(",", ":")).encode()
    is_dirty = bool(status.stdout)
    return {
        "name": name,
        "revision": revision,
        "classification": "dirty-unreviewed" if is_dirty else "clean",
        "worktreeStatusSha256": hashlib.sha256(status.stdout).hexdigest(),
        "trackedDiffSha256": hashlib.sha256(diff.stdout).hexdigest(),
        "dirtyFilesSha256": hashlib.sha256(dirty_payload).hexdigest(),
        "dirtyFileCount": len(dirty_files),
    }


def command_version(*command: str) -> str | None:
    try:
        result = subprocess.run(command, capture_output=True, text=True, check=False)
    except OSError:
        return None
    if result.returncode:
        return None
    output = (result.stdout or result.stderr).strip()
    return output.splitlines()[0] if output else None


def create_receipt(args: argparse.Namespace) -> dict[str, Any]:
    run_dir = args.run_dir.resolve()
    if (
        not isinstance(args.callsign, str)
        or not args.callsign
        or any(
            ord(character) < 33 or ord(character) == 127 or character in ".*>"
            for character in args.callsign
        )
    ):
        raise ValueError("callsign must be a single non-empty NATS subject token")
    ports = {
        "nats": args.nats_port,
        "qgcUdpListen": args.qgc_port,
        "bridgeUdpBind": args.bridge_port,
    }
    invalid_ports = [
        name
        for name, port in ports.items()
        if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535
    ]
    if invalid_ports:
        raise ValueError("ports must be integers from 1 to 65535: " + ", ".join(invalid_ports))
    if args.qgc_port == args.bridge_port:
        raise ValueError("qgc and bridge UDP ports must be different")
    config = {
        "natsUrl": f"nats://127.0.0.1:{args.nats_port}",
        "callsign": args.callsign,
        "qgcUdpListen": f"127.0.0.1:{args.qgc_port}",
        "bridgeUdpBind": f"127.0.0.1:{args.bridge_port}",
        "systemId": 1,
        "homeAltitudeMsl": 82,
        "profile": "interactive-antoine-gazebo",
    }
    runtime_config_text = json.dumps(config, indent=2, sort_keys=True) + "\n"
    sources = {
        "antoine": source_record("antoine", args.antoine_root),
        "gazeboAssets": source_record("gazeboAssets", args.sim_root),
        "qgroundcontrol": source_record("qgroundcontrol", args.qgc_root),
    }
    receipt = {
        "contract": "damn-vulnerable-drone.interactive-profile",
        "version": "1.0.0",
        "runId": run_dir.name,
        "createdAtUtc": datetime.now(UTC).isoformat(),
        "status": "starting",
        "profile": "interactive-antoine-gazebo",
        "runDirectory": str(run_dir),
        "flightRunDirectory": str(run_dir / "flight-proof"),
        "callsign": args.callsign,
        "ports": {
            "nats": args.nats_port,
            "qgcUdpListen": args.qgc_port,
            "bridgeUdpBind": args.bridge_port,
        },
        "source": sources,
        "artifacts": {
            "qgroundcontrolBinarySha256": sha256(args.qgc_binary),
            "bridgeBinarySha256": sha256(args.bridge_binary),
            "antoineNatsBinarySha256": sha256(args.antoine_nats_binary),
            "antoineAuthorityBinarySha256": sha256(args.antoine_authority_binary),
            "antoineBoundaryBinarySha256": sha256(args.antoine_boundary_binary),
            "gazeboBinarySha256": sha256(args.gazebo_binary),
            "patchSha256": sha256(args.qgc_patch),
            "runtimeWorldSha256": sha256(args.runtime_world),
            "runtimeConfigSha256": hashlib.sha256(runtime_config_text.encode()).hexdigest(),
            "qgcSettingsSha256": sha256(args.qgc_settings),
            "guiConfigSha256": sha256(args.gui_config),
        },
        "toolchain": {
            "rustc": command_version("rustc", "--version"),
            "cargo": command_version("cargo", "--version"),
            "gazebo": command_version(str(args.gazebo_binary), "--version"),
        },
        "processes": {},
    }
    _atomic_write_text(run_dir / "interactive-profile.json", runtime_config_text)
    return receipt


def _atomic_write_text(path: Path, value: str) -> None:
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            temporary.write(value)
        os.replace(temporary_path, path)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--antoine-root", type=Path, required=True)
    parser.add_argument("--sim-root", type=Path, required=True)
    parser.add_argument("--qgc-root", type=Path, required=True)
    parser.add_argument("--qgc-binary", type=Path, required=True)
    parser.add_argument("--bridge-binary", type=Path, required=True)
    parser.add_argument("--antoine-nats-binary", type=Path, required=True)
    parser.add_argument("--antoine-authority-binary", type=Path, required=True)
    parser.add_argument("--antoine-boundary-binary", type=Path, required=True)
    parser.add_argument("--gazebo-binary", type=Path, required=True)
    parser.add_argument("--qgc-patch", type=Path, required=True)
    parser.add_argument("--runtime-world", type=Path, required=True)
    parser.add_argument("--qgc-settings", type=Path, required=True)
    parser.add_argument("--gui-config", type=Path, required=True)
    parser.add_argument("--callsign", required=True)
    parser.add_argument("--nats-port", type=int, required=True)
    parser.add_argument("--qgc-port", type=int, required=True)
    parser.add_argument("--bridge-port", type=int, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        receipt = create_receipt(args)
        _atomic_write_text(args.output, json.dumps(receipt, indent=2, sort_keys=True) + "\n")
    except (OSError, subprocess.SubprocessError, ValueError) as error:
        parser.error(str(error))
    print(json.dumps({"runId": receipt["runId"], "status": receipt["status"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
