#!/usr/bin/env python3
"""Record the toolchain, source inputs, commands, and run-owned Antoine binaries."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

BINARIES = (
    "antoine_sitl_nats",
    "antoine_gz_local_authority",
    "antoine-boundary-service",
)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def command_version(*command: str) -> str:
    result = subprocess.run(command, check=True, capture_output=True, text=True)
    return (result.stdout or result.stderr).strip()


def source_manifest(antoine_root: Path) -> list[dict[str, Any]]:
    snapshot_receipt = antoine_root.parent / "antoine-source-snapshot.json"
    if snapshot_receipt.is_file() and not snapshot_receipt.is_symlink():
        snapshot = json.loads(snapshot_receipt.read_text())
        files = []
        seen = set()
        for item in snapshot["files"]:
            relative = Path(item["path"])
            if relative.is_absolute() or ".." in relative.parts or relative in seen:
                raise ValueError("Unsafe or duplicate snapshotted build input")
            path = antoine_root.parent / relative
            if path.is_symlink() or not path.is_file() or sha256(path) != item["sha256"]:
                raise ValueError("Build source differs from its frozen snapshot receipt")
            seen.add(relative)
            files.append(
                {
                    "path": relative.as_posix(),
                    "sizeBytes": path.stat().st_size,
                    "sha256": item["sha256"],
                }
            )
        if not files:
            raise ValueError("Empty build source snapshot")
        return sorted(files, key=lambda item: item["path"])
    roots = (
        antoine_root / "sitl" / "antoine-sitl",
        antoine_root / "runtime" / "antoine-boundary",
        antoine_root.parent / "ew-wire-contract",
    )
    files: set[Path] = set()
    for root in roots:
        if not root.is_dir():
            raise ValueError(f"Missing Antoine build source directory: {root}")
        for path in root.rglob("*"):
            if path.is_symlink():
                raise ValueError(f"Symlink in Antoine build source inputs: {path}")
            if path.is_file() and (
                path.suffix in {".rs", ".toml", ".json"} or path.name == "Cargo.lock"
            ):
                files.add(path)
    return [
        {
            "path": path.relative_to(antoine_root.parent).as_posix(),
            "sizeBytes": path.stat().st_size,
            "sha256": sha256(path),
        }
        for path in sorted(files)
    ]


def source_digest(files: list[dict[str, Any]]) -> str:
    digest = hashlib.sha256()
    for item in files:
        digest.update(item["path"].encode("utf-8"))
        digest.update(b"\0")
        digest.update(item["sha256"].encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def build_receipt(
    antoine_root: Path,
    binary_dir: Path,
    run_id: str,
    source_revision: str,
    native_receipt: Path | None = None,
) -> dict[str, Any]:
    antoine_root = antoine_root.resolve(strict=True)
    binary_dir = binary_dir.resolve(strict=True)
    source_files = source_manifest(antoine_root)
    artifacts = []
    for name in BINARIES:
        path = binary_dir / name
        if path.is_symlink() or not path.is_file() or not path.stat().st_mode & 0o111:
            raise ValueError(f"Missing or non-executable run-owned binary: {path}")
        artifacts.append(
            {
                "path": f"antoine-binaries/{name}",
                "sizeBytes": path.stat().st_size,
                "sha256": sha256(path),
            }
        )
    result = {
        "contract": "damn-vulnerable-drone.antoine-run-build",
        "version": "1.0.0",
        "runId": run_id,
        "createdAtUtc": datetime.now(UTC).isoformat(),
        "status": "recorded",
        "toolchain": {
            "cargo": command_version("cargo", "--version"),
            "rustc": command_version("rustc", "--version", "--verbose"),
        },
        "buildWorkingDirectory": "antoine-source-snapshot/antoine",
        "buildEnvironment": {
            "RUSTUP_TOOLCHAIN": os.environ.get("RUSTUP_TOOLCHAIN", "default"),
            "CARGO_NET_OFFLINE": "true",
        },
        "sourceRevision": source_revision,
        "sourceFiles": source_files,
        "sourceManifestSha256": source_digest(source_files),
        "buildCommands": [
            [
                "cargo",
                "build",
                "--locked",
                "--offline",
                "--manifest-path",
                "sitl/antoine-sitl/Cargo.toml",
                "--features",
                "nats",
                "--bin",
                "antoine_sitl_nats",
                "--bin",
                "antoine_gz_local_authority",
            ],
            [
                "cargo",
                "build",
                "--locked",
                "--offline",
                "--manifest-path",
                "runtime/antoine-boundary/Cargo.toml",
                "--features",
                "service",
                "--bin",
                "antoine-boundary-service",
            ],
        ],
        "artifacts": artifacts,
    }
    if native_receipt is not None:
        native = json.loads(native_receipt.read_text())
        build = native["build"]
        support = build["support_artifacts"]
        expected = {
            "antoine_sitl_nats": build["artifact_sha256"],
            "antoine_gz_local_authority": support["antoine_gz_local_authority"]["artifact_sha256"],
            "antoine-boundary-service": support["antoine-boundary-service"]["artifact_sha256"],
        }
        if any(item["sha256"] != expected[Path(item["path"]).name] for item in artifacts):
            raise ValueError("Copied artifacts differ from the native build receipt")
        result["nativeBuildReceiptSha256"] = sha256(native_receipt)
        result["buildCommands"] = [
            build["command"][1:],
            support["antoine-boundary-service"]["command"],
        ]
        result["buildEnvironment"]["CARGO_TARGET_DIR"] = build["target_dir"]
        result["buildEnvironment"]["CARGO_NET_OFFLINE"] = os.environ.get(
            "CARGO_NET_OFFLINE", "false"
        )
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--antoine-root", type=Path, required=True)
    parser.add_argument("--binary-dir", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--source-revision", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--native-build-receipt", type=Path)
    args = parser.parse_args()
    try:
        receipt = build_receipt(
            args.antoine_root,
            args.binary_dir,
            args.run_id,
            args.source_revision,
            args.native_build_receipt,
        )
    except (OSError, subprocess.CalledProcessError, ValueError, KeyError, TypeError) as error:
        print(f"Antoine run build receipt failed: {error}", file=sys.stderr)
        return 2
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "status": receipt["status"],
                "sourceManifestSha256": receipt["sourceManifestSha256"],
                "artifacts": receipt["artifacts"],
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
