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

from dvd_research.nats_identity import valid_callsign
from dvd_research.strict_json import loads as strict_json_loads
from gazebo_version import gazebo_version


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


def snapshot_record(run_dir: Path, snapshot_dir: Path, filename: str, contract: str) -> dict:
    run_dir = run_dir.resolve(strict=True)
    if snapshot_dir.is_symlink():
        raise ValueError("source snapshot must be a run-owned directory")
    snapshot_dir = snapshot_dir.resolve(strict=True)
    if not snapshot_dir.is_dir() or not snapshot_dir.is_relative_to(run_dir):
        raise ValueError("source snapshot must be a run-owned directory")
    receipt_path = snapshot_dir / filename
    if receipt_path.is_symlink() or not receipt_path.is_file():
        raise ValueError(f"source snapshot receipt is missing: {receipt_path}")

    document = strict_json_loads(receipt_path.read_text(encoding="utf-8"))
    if (
        not isinstance(document, dict)
        or document.get("version") != "1.1.0"
        or document.get("contract") != contract
        or document.get("status") != "verified"
        or not isinstance(document.get("snapshotManifestSha256"), str)
        or not isinstance(document.get("files"), list)
        or not document["files"]
    ):
        raise ValueError(f"source snapshot receipt is incomplete: {receipt_path}")
    manifest = hashlib.sha256()
    seen: set[Path] = set()
    for entry in document["files"]:
        if not isinstance(entry, dict):
            raise ValueError(f"source snapshot manifest is invalid: {receipt_path}")
        raw_path = entry.get("path")
        expected_size = entry.get("sizeBytes")
        expected_hash = entry.get("sha256")
        if (
            set(entry) != {"path", "sizeBytes", "sha256"}
            or not isinstance(raw_path, str)
            or not raw_path
            or "\\" in raw_path
            or "\0" in raw_path
        ):
            raise ValueError(f"source snapshot manifest is invalid: {receipt_path}")
        relative = Path(raw_path)
        if (
            relative.is_absolute()
            or relative.as_posix() != raw_path
            or ".." in relative.parts
            or relative in seen
        ):
            raise ValueError(f"source snapshot manifest has an unsafe path: {receipt_path}")
        source = snapshot_dir / relative
        try:
            resolved_source = source.resolve(strict=True)
        except OSError as error:
            raise ValueError(f"source snapshot file is missing: {source}") from error
        content_hash = sha256(source)
        if (
            not resolved_source.is_relative_to(snapshot_dir)
            or content_hash is None
            or content_hash != expected_hash
            or type(expected_size) is not int
            or expected_size < 0
            or source.stat().st_size != expected_size
        ):
            raise ValueError(f"source snapshot bytes do not match their receipt: {source}")
        seen.add(relative)
        manifest.update(relative.as_posix().encode("utf-8"))
        manifest.update(b"\0")
        manifest.update(content_hash.encode("ascii"))
        manifest.update(b"\n")
    if manifest.hexdigest() != document["snapshotManifestSha256"]:
        raise ValueError(f"source snapshot manifest digest is invalid: {receipt_path}")
    return {
        "path": snapshot_dir.relative_to(run_dir).as_posix(),
        "receiptSha256": sha256(receipt_path),
        "snapshotManifestSha256": document["snapshotManifestSha256"],
        "fileCount": len(document["files"]),
    }


def create_receipt(args: argparse.Namespace) -> dict[str, Any]:
    run_dir = args.run_dir.resolve()
    if not valid_callsign(args.callsign):
        raise ValueError(
            "callsign must be 1..64 ASCII letters, digits, '-' or '_' (one NATS subject token)"
        )
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
    profile = (
        "headless-antoine-gazebo-mavlink"
        if args.qgc_binary is None
        else "interactive-antoine-gazebo"
    )
    config = {
        "natsUrl": f"nats://127.0.0.1:{args.nats_port}",
        "callsign": args.callsign,
        "qgcUdpListen": f"127.0.0.1:{args.qgc_port}",
        "bridgeUdpBind": f"127.0.0.1:{args.bridge_port}",
        "systemId": 1,
        "homeAltitudeMsl": 82,
        "profile": profile,
    }
    runtime_config_text = json.dumps(config, indent=2, sort_keys=True) + "\n"
    sources = {
        "antoine": source_record("antoine", args.antoine_root),
        "gazeboAssets": source_record("gazeboAssets", args.sim_root),
    }
    if args.qgc_root is not None:
        sources["qgroundcontrol"] = source_record("qgroundcontrol", args.qgc_root)
    antoine_snapshot = getattr(args, "antoine_source_snapshot", None)
    gazebo_snapshot = getattr(args, "gazebo_source_snapshot", None)
    source_snapshots = None
    if antoine_snapshot is not None or gazebo_snapshot is not None:
        if antoine_snapshot is None or gazebo_snapshot is None:
            raise ValueError("both Antoine and Gazebo source snapshots are required together")
        source_snapshots = {
            "antoine": snapshot_record(
                run_dir,
                antoine_snapshot,
                "antoine-source-snapshot.json",
                "damn-vulnerable-drone.antoine-source-snapshot",
            ),
            "gazebo": snapshot_record(
                run_dir,
                gazebo_snapshot,
                "gazebo-source-snapshot.json",
                "damn-vulnerable-drone.gazebo-source-snapshot",
            ),
        }
    receipt = {
        "contract": "damn-vulnerable-drone.interactive-profile",
        "version": "1.0.0",
        "runId": run_dir.name,
        "createdAtUtc": datetime.now(UTC).isoformat(),
        "status": "starting",
        "profile": profile,
        "runDirectory": str(run_dir),
        "flightRunDirectory": str(run_dir / "flight-proof"),
        "callsign": args.callsign,
        "ports": {
            "nats": args.nats_port,
            "qgcUdpListen": args.qgc_port,
            "bridgeUdpBind": args.bridge_port,
        },
        "source": sources,
        "sourceSnapshots": source_snapshots,
        "artifacts": {
            "qgroundcontrolBinarySha256": sha256(args.qgc_binary) if args.qgc_binary else None,
            "bridgeBinarySha256": sha256(args.bridge_binary),
            "antoineNatsBinarySha256": sha256(args.antoine_nats_binary),
            "antoineAuthorityBinarySha256": sha256(args.antoine_authority_binary),
            "antoineBoundaryBinarySha256": sha256(args.antoine_boundary_binary),
            "gazeboBinarySha256": sha256(args.gazebo_binary),
            "patchSha256": sha256(args.qgc_patch),
            "runtimeWorldSha256": sha256(args.runtime_world),
            "runtimeConfigSha256": hashlib.sha256(runtime_config_text.encode()).hexdigest(),
            "qgcSettingsSha256": sha256(args.qgc_settings) if args.qgc_settings else None,
            "guiConfigSha256": sha256(args.gui_config),
        },
        "toolchain": {
            "rustc": command_version("rustc", "--version"),
            "cargo": command_version("cargo", "--version"),
            "gazebo": gazebo_version(args.gazebo_binary),
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
    parser.add_argument("--qgc-root", type=Path)
    parser.add_argument("--qgc-binary", type=Path)
    parser.add_argument("--bridge-binary", type=Path, required=True)
    parser.add_argument("--antoine-nats-binary", type=Path, required=True)
    parser.add_argument("--antoine-authority-binary", type=Path, required=True)
    parser.add_argument("--antoine-boundary-binary", type=Path, required=True)
    parser.add_argument("--gazebo-binary", type=Path, required=True)
    parser.add_argument("--qgc-patch", type=Path, required=True)
    parser.add_argument("--runtime-world", type=Path, required=True)
    parser.add_argument("--qgc-settings", type=Path)
    parser.add_argument("--gui-config", type=Path, required=True)
    parser.add_argument("--antoine-source-snapshot", type=Path)
    parser.add_argument("--gazebo-source-snapshot", type=Path)
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
