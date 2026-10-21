"""Seal and archive task-owned source bundles for Antoine's native receipt verifier."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from snapshot_antoine_build_inputs import sha256


def verified_files(snapshot: Path) -> list[tuple[Path, Path]]:
    receipt = json.loads((snapshot / "antoine-source-snapshot.json").read_text())
    files = []
    seen = set()
    for entry in receipt["files"]:
        path = Path(entry["path"])
        if path.is_absolute() or ".." in path.parts or ".git" in path.parts or path in seen:
            raise ValueError("Unsafe or duplicate source snapshot path")
        if not path.parts or path.parts[0].startswith("."):
            raise ValueError("Unexpected source snapshot root")
        source = snapshot / path
        if source.is_symlink() or not source.is_file() or sha256(source) != entry["sha256"]:
            raise ValueError("Source snapshot bytes differ from their receipt")
        seen.add(path)
        files.append((source, path))
    if not files:
        raise ValueError("Empty source snapshot")
    return files


def seal(snapshot: Path, target: Path) -> dict:
    snapshot, target = snapshot.resolve(), target.resolve(strict=True)
    files = verified_files(snapshot)
    names = {relative.parts[0] for _, relative in files}
    if not {"antoine", "ew-wire-contract"}.issubset(names):
        raise ValueError("Missing required source snapshot roots")
    roots = [snapshot / name for name in sorted(names)]
    if not target.is_dir() or any((root / ".git").exists() for root in roots):
        raise ValueError("Sealing requires fresh source roots and a run-owned build directory")
    environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    environment.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
    commits = {}
    for root in roots:
        paths = [
            path.relative_to(root.name).as_posix()
            for _, path in files
            if path.parts[0] == root.name
        ]
        if not paths:
            raise ValueError("Source snapshot root is empty")

        def git(*arguments: str, repository: Path = root) -> str:
            return subprocess.check_output(
                ["git", "-C", str(repository), *arguments],
                env=environment,
                text=True,
                stderr=subprocess.PIPE,
            ).strip()

        git("-c", "init.defaultBranch=lab-snapshot", "init", "--quiet", "--template=")
        (root / ".git/info").mkdir(exist_ok=True)
        (root / ".git/info/exclude").write_text(
            ".cargo-target/\ntarget/\n__pycache__/\n.DS_Store\n"
        )
        git("add", "--", *paths)
        git(
            "-c",
            "user.name=DVD lab snapshot",
            "-c",
            "user.email=dvd-lab@localhost",
            "-c",
            "commit.gpgSign=false",
            "-c",
            "core.hooksPath=/dev/null",
            "commit",
            "--quiet",
            "--no-verify",
            "-m",
            "Frozen local drone lab source snapshot",
        )
        commits[root.name] = git("rev-parse", "HEAD")
        if git("status", "--porcelain=v1", "--untracked-files=all"):
            raise ValueError("Source snapshot did not seal cleanly")
    native_target = snapshot / "antoine/.cargo-target/antoine-sitl-build-receipt"
    native_target.parent.mkdir(exist_ok=True)
    native_target.symlink_to(target, target_is_directory=True)
    receipt = {
        "contract": "damn-vulnerable-drone.sealed-source-snapshot",
        "version": "1.0.0",
        "commits": commits,
        "sourceSnapshotSha256": sha256(snapshot / "antoine-source-snapshot.json"),
    }
    (snapshot / "native-source-git.json").write_text(json.dumps(receipt, indent=2) + "\n")
    return receipt


def archive(snapshot: Path, destination: Path) -> None:
    snapshot = snapshot.resolve()
    files = verified_files(snapshot)
    if destination.exists() or destination.is_symlink():
        raise ValueError("Refusing to overwrite an archived source snapshot")
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".source-archive-", dir=destination.parent))
    try:
        for source, path in files:
            target = staging / path
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
            shutil.copystat(source, target)
            if sha256(target) != sha256(source):
                raise ValueError("Source changed during archive copy")
        for name in ("antoine-source-snapshot.json", "native-source-git.json"):
            if (snapshot / name).is_file():
                shutil.copyfile(snapshot / name, staging / name)
        verified_files(staging)
        os.replace(staging, destination)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("seal", "archive"))
    parser.add_argument("snapshot", type=Path)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    try:
        if args.operation == "seal":
            print(json.dumps(seal(args.snapshot, args.destination)))
        else:
            archive(args.snapshot, args.destination)
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        parser.exit(2, f"Source snapshot {args.operation} failed: {error}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
