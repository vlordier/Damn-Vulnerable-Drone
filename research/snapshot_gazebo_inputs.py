#!/usr/bin/env python3
"""Create a run-owned, content-verified snapshot of Gazebo flight inputs."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Iterable
from pathlib import Path

INPUTS = (
    Path("worlds/antoine-gz-flight-proof.sdf"),
    Path("models"),
    Path("tools/gz-sitl-applier"),
    Path("applier-build-source"),
    Path("scripts/build_gz_sitl_applier.py"),
    Path("scripts/gazebo_environment.sh"),
    Path("scripts/strict_json.py"),
)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def source_files(source_root: Path) -> list[tuple[Path, Path]]:
    files: list[tuple[Path, Path]] = []
    for relative in INPUTS:
        source = source_root / relative
        if relative == Path("applier-build-source") and not source.exists():
            continue
        if relative in (
            Path("models"),
            Path("tools/gz-sitl-applier"),
            Path("applier-build-source"),
        ):
            if not source.is_dir() or source.is_symlink():
                raise ValueError(f"Gazebo model asset directory is missing or unsafe: {source}")
            candidates: Iterable[Path] = source.rglob("*")
            for candidate in candidates:
                if candidate.is_symlink():
                    raise ValueError(
                        f"Symlink in Gazebo model assets is not snapshot-safe: {candidate}"
                    )
                if candidate.is_file():
                    files.append((candidate, candidate.relative_to(source_root)))
        else:
            if source.is_symlink() or not source.is_file():
                raise ValueError(f"Required Gazebo flight input is missing or unsafe: {source}")
            files.append((source, relative))
    return sorted(files, key=lambda item: item[1].as_posix())


def file_manifest(source_root: Path) -> list[dict[str, object]]:
    return [
        {
            "path": relative.as_posix(),
            "sizeBytes": source.stat().st_size,
            "sha256": sha256_file(source),
        }
        for source, relative in source_files(source_root)
    ]


def manifest_digest(files: list[dict[str, object]]) -> str:
    digest = hashlib.sha256()
    for item in files:
        digest.update(str(item["path"]).encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(item["sha256"]).encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def build_applier(source_root: Path, staging: Path, patch_path: Path) -> dict[str, object]:
    if patch_path.is_symlink() or not patch_path.is_file():
        raise ValueError("Applier source patch must be a regular file")
    patch = patch_path.read_bytes()
    targets = []
    for line in patch.decode().splitlines():
        if line.startswith(("--- ", "+++ ")):
            value = line[4:].split("\t", 1)[0]
            if value == "/dev/null" and line.startswith("--- "):
                continue
            prefix = "a/" if line.startswith("--- ") else "b/"
            if not value.startswith(prefix):
                raise ValueError("Invalid applier patch path")
            path = Path(value[2:])
            if (
                path.is_absolute()
                or ".." in path.parts
                or path.parent != Path("tools/gz-sitl-applier")
                or path.suffix not in {".cc", ".hh"}
            ):
                raise ValueError("Applier patch must only change applier C++ source files")
            targets.append(path)
    if not targets:
        raise ValueError("Empty applier source patch")
    build_root = staging / "applier-build-source"
    if build_root.exists():
        raise ValueError("Refusing an already populated applier build directory")
    sources = sorted((source_root / "tools/gz-sitl-applier").glob("*.cc"))
    sources += sorted((source_root / "tools/gz-sitl-applier").glob("*.hh"))
    sources += [
        source_root / "tools/gz-sitl-applier/build.sh",
        source_root / "scripts/gazebo_environment.sh",
    ]
    before = []
    for source in sources:
        if source.is_symlink() or not source.is_file():
            raise ValueError("Missing or unsafe applier build input")
        relative = source.relative_to(source_root)
        before.append({"path": relative.as_posix(), "sha256": sha256_file(source)})
        target = build_root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        if sha256_file(target) != before[-1]["sha256"]:
            raise RuntimeError("Applier source changed during snapshot")
    command = ["patch", "--batch", "--fuzz=0", "-V", "never", "-p1"]
    result = subprocess.run(
        command + ["--forward", "--dry-run"],
        input=patch,
        cwd=build_root,
        capture_output=True,
        timeout=10,
    )
    if result.returncode == 0:
        subprocess.run(
            command + ["--forward"],
            input=patch,
            cwd=build_root,
            capture_output=True,
            timeout=10,
            check=True,
        )
        patch_status = "applied"
    else:
        reversed_patch = subprocess.run(
            command + ["--reverse", "--dry-run"],
            input=patch,
            cwd=build_root,
            capture_output=True,
            timeout=10,
        )
        if reversed_patch.returncode:
            raise ValueError("Applier patch does not match this Gazebo source revision")
        patch_status = "already-present"
    (build_root / "applier.patch").write_bytes(patch)
    output = staging / "tools/gz-sitl-applier/gz-sitl-applier"
    environment = {**os.environ, "FURIA_GZ_APPLIER_BUILD_OUTPUT": str(output)}
    try:
        subprocess.run(
            ["bash", str(build_root / "tools/gz-sitl-applier/build.sh")],
            env=environment,
            cwd=build_root,
            timeout=180,
            check=True,
            capture_output=True,
        )
    except subprocess.CalledProcessError as error:
        raise RuntimeError(
            "Applier build failed: " + error.stderr.decode(errors="replace")[-8000:]
        ) from error
    if any(sha256_file(source_root / item["path"]) != item["sha256"] for item in before):
        raise RuntimeError("Applier source changed during build")
    if output.is_symlink() or not output.is_file() or not os.access(output, os.X_OK):
        raise ValueError("Applier build did not produce an executable")
    receipt = {
        "contract": "damn-vulnerable-drone.applier-build",
        "version": "1.0.0",
        "status": "built",
        "sourceFiles": before,
        "patchSha256": hashlib.sha256(patch).hexdigest(),
        "patchStatus": patch_status,
        "binarySha256": sha256_file(output),
    }
    (build_root / "build-receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
    return receipt


def create_snapshot(
    source_root: Path, destination: Path, applier_patch: Path | None = None
) -> dict[str, object]:
    source_root = source_root.resolve(strict=True)
    if destination.exists() or destination.is_symlink():
        raise ValueError(f"Refusing to overwrite Gazebo snapshot destination: {destination}")
    before = file_manifest(source_root)
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{destination.name}.", dir=destination.parent))
    try:
        for source, relative in source_files(source_root):
            target = staging / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
            shutil.copystat(source, target, follow_symlinks=False)
        copied = [
            {
                "path": item["path"],
                "sizeBytes": (staging / str(item["path"])).stat().st_size,
                "sha256": sha256_file(staging / str(item["path"])),
            }
            for item in before
        ]
        after = file_manifest(source_root)
        if before != copied or before != after:
            raise RuntimeError(
                "Gazebo inputs changed while the run-owned snapshot was being created"
            )
        applier_build = None
        if applier_patch is not None:
            applier_build = build_applier(source_root, staging, applier_patch)
            copied = file_manifest(staging)
        receipt: dict[str, object] = {
            "contract": "damn-vulnerable-drone.gazebo-source-snapshot",
            "version": "1.1.0",
            "sourceRootLabel": source_root.name,
            "sourceManifestSha256": manifest_digest(before),
            "snapshotManifestSha256": manifest_digest(copied),
            "files": copied,
            "status": "verified",
            "applierBuild": applier_build,
        }
        (staging / "gazebo-source-snapshot.json").write_text(
            json.dumps(receipt, indent=2) + "\n", encoding="utf-8"
        )
        os.replace(staging, destination)
        return receipt
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--applier-patch", type=Path)
    args = parser.parse_args()
    try:
        receipt = create_snapshot(args.source, args.destination, args.applier_patch)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        print(f"Gazebo source snapshot failed: {error}", file=sys.stderr)
        return 2
    print(json.dumps({key: receipt[key] for key in ("status", "snapshotManifestSha256", "files")}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
