#!/usr/bin/env python3
"""Snapshot Antoine's exact SITL/boundary build graph into a run-owned tree."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path
from typing import Any

SOURCE_ROOTS = (
    Path("sitl/antoine-sitl"),
    Path("runtime/antoine-boundary"),
    Path("verification/fixtures"),
    Path("tests"),
    Path("scripts"),
)
IGNORED_DIRS = {".git", ".beads", "__pycache__", "build", "target"}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def local_dependency_files(root: Path, ew_wire_root: Path) -> list[tuple[Path, Path]]:
    """Capture the local Cargo path graph, retaining its workspace-relative layout."""
    root = root.resolve()
    ew_wire_root = ew_wire_root.resolve()
    pending = [root / "sitl/antoine-sitl/Cargo.toml", root / "runtime/antoine-boundary/Cargo.toml"]
    visited = set()
    packages: dict[Path, Path] = {}
    while pending:
        manifest_path = pending.pop().resolve()
        if manifest_path in visited:
            continue
        visited.add(manifest_path)
        if len(visited) > 64:
            raise ValueError("Local Cargo dependency graph exceeds 64 packages")
        with manifest_path.open("rb") as stream:
            document = tomllib.load(stream)
        tables = [
            document.get(name, {})
            for name in ("dependencies", "build-dependencies", "dev-dependencies")
        ]
        for target in document.get("target", {}).values():
            tables.extend(
                target.get(name, {})
                for name in ("dependencies", "build-dependencies", "dev-dependencies")
            )
        for table in tables:
            for dependency in table.values():
                if not isinstance(dependency, dict) or not isinstance(dependency.get("path"), str):
                    continue
                package = (manifest_path.parent / dependency["path"]).resolve()
                if package == (root.parent / "ew-wire-contract").resolve():
                    package, relative = ew_wire_root, Path("ew-wire-contract")
                elif package.is_relative_to(ew_wire_root.resolve()):
                    relative = Path("ew-wire-contract") / package.relative_to(
                        ew_wire_root.resolve()
                    )
                elif package.is_relative_to(root):
                    relative = Path("antoine") / package.relative_to(root)
                elif package.is_relative_to(root.parent):
                    relative = package.relative_to(root.parent)
                else:
                    raise ValueError(
                        f"Local Cargo dependency is outside the source workspace: {package}"
                    )
                if not (package / "Cargo.toml").is_file():
                    raise ValueError(f"Missing local Cargo dependency: {relative}")
                packages[package] = relative
                pending.append(package / "Cargo.toml")
    files = []
    for package, relative in packages.items():
        # Shared contract crates retain their conformance fixtures referenced
        # by include_str!("../../fixtures/...") in the Rust test module.
        context = (
            package.parent
            if package.name == "rust" and (package.parent / "fixtures").is_dir()
            else package
        )
        destination = relative.parent if context != package else relative
        for source in context.rglob("*"):
            if IGNORED_DIRS.intersection(source.relative_to(context).parts):
                continue
            if source.is_symlink():
                if source.resolve() == context:
                    continue
                raise ValueError("Symlink in local Cargo dependency source")
            if source.is_file():
                files.append((source, destination / source.relative_to(context)))
    return files


def source_files(
    root: Path, ew_wire_root: Path, lock_overrides: dict[str, Path] | None = None
) -> list[tuple[Path, Path]]:
    files: list[tuple[Path, Path]] = []
    for relative in SOURCE_ROOTS:
        source = root / relative
        if relative == Path("tests") and not source.exists():
            candidates = ()
        else:
            if not source.is_dir() or source.is_symlink():
                raise ValueError(f"Missing or unsafe Antoine build input directory: {source}")
            candidates = source.rglob("*")
        for candidate in candidates:
            if IGNORED_DIRS.intersection(candidate.relative_to(root).parts):
                continue
            if candidate.is_symlink():
                raise ValueError(
                    f"Symlink in Antoine build inputs is not snapshot-safe: {candidate}"
                )
            if candidate.is_file():
                files.append((candidate, Path("antoine") / candidate.relative_to(root)))
    if not ew_wire_root.is_dir() or ew_wire_root.is_symlink():
        raise ValueError(f"Missing or unsafe ew-wire-contract source directory: {ew_wire_root}")
    for candidate in ew_wire_root.rglob("*"):
        if IGNORED_DIRS.intersection(candidate.relative_to(ew_wire_root).parts):
            continue
        if candidate.is_symlink():
            if candidate.resolve() == ew_wire_root:
                continue
            raise ValueError(f"Symlink in ew-wire-contract is not snapshot-safe: {candidate}")
        if candidate.is_file():
            files.append(
                (candidate, Path("ew-wire-contract") / candidate.relative_to(ew_wire_root))
            )
    files.extend(local_dependency_files(root, ew_wire_root))
    files = [
        (source, relative)
        for relative, source in {relative: source for source, relative in files}.items()
    ]
    for relative, source in (lock_overrides or {}).items():
        if source.is_symlink() or not source.is_file():
            raise ValueError(f"Missing or unsafe versioned Antoine lockfile: {source}")
        destination = Path(relative)
        files = [item for item in files if item[1] != destination]
        files.append((source, destination))
    return sorted(files, key=lambda item: item[1].as_posix())


def manifest(
    root: Path, ew_wire_root: Path, lock_overrides: dict[str, Path] | None = None
) -> list[dict[str, Any]]:
    return [
        {"path": relative.as_posix(), "sizeBytes": source.stat().st_size, "sha256": sha256(source)}
        for source, relative in source_files(root, ew_wire_root, lock_overrides)
    ]


def manifest_digest(files: list[dict[str, Any]]) -> str:
    digest = hashlib.sha256()
    for item in files:
        digest.update(item["path"].encode("utf-8"))
        digest.update(b"\0")
        digest.update(item["sha256"].encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def patch_changes_present(hunks: list[bytes], source_root: Path) -> bool:
    """Verify patch changes despite shifted upstream context, without editing source."""
    for hunk in hunks:
        lines = hunk.decode("utf-8").splitlines()
        target_line = next((line for line in lines if line.startswith("+++ b/")), None)
        if target_line is None:
            return False
        target = source_root / target_line[6:]
        try:
            current_lines = set(target.read_text(encoding="utf-8").splitlines())
        except OSError:
            return False
        additions = [
            line[1:] for line in lines if line.startswith("+") and not line.startswith("+++")
        ]
        removals = [
            line[1:] for line in lines if line.startswith("-") and not line.startswith("---")
        ]
        if not additions or any(line not in current_lines for line in additions):
            return False
        if any(line in current_lines for line in removals):
            return False
    return True


def patch_hunk_superseded_by_upstream(hunk: bytes, source_root: Path) -> bool:
    """Accept the newer buffered grant gate that replaces receive-time checks."""
    lines = hunk.decode("utf-8", errors="replace").splitlines()
    target_line = next((line for line in lines if line.startswith("+++ b/")), None)
    if target_line != "+++ b/sitl/antoine-sitl/src/bin/antoine_sitl_nats.rs":
        return False
    additions = [line[1:] for line in lines if line.startswith("+") and not line.startswith("+++")]
    removals = [line[1:] for line in lines if line.startswith("-") and not line.startswith("---")]
    receiver_gate_hunk = any(
        "core_grant.for_command().await" in line or "let core_grant = core_grant.clone();" in line
        for line in additions
    )
    moved_grant_hunk = any(
        line.strip() == "let core_grant = Arc::new(core_lease::CoreLease::new());"
        for line in removals
    )
    if not receiver_gate_hunk and not moved_grant_hunk:
        return False
    source = source_root / target_line[6:]
    try:
        current = source.read_text(encoding="utf-8")
    except OSError:
        return False
    # Newer Antoine revisions hold a candidate separately, then admit it only
    # through current grant and Vigie validation in the flight-control loop.
    # This subsumes the older patch's receive-time authorization and declaration
    # move while preserving fail-closed behavior at the consumer.
    return all(
        marker in current
        for marker in (
            "struct MissionSetpointBuffer",
            "fn authorized_for(",
            "fn mission_setpoint_matches_live_authorization(",
            "buffer.authorized_for(&grants, &admissions, &callsign, &world_frame_id, now_ms)",
            "state=pending-authorization",
            "fn mission_setpoint_parser_requires_fresh_bound_fields_and_vehicle()",
            "fn waypoint_handoff_keeps_old_pair_until_new_grant_and_setpoint_both_arrive()",
        )
    )


def create_snapshot(
    source_root: Path,
    ew_wire_root: Path,
    destination: Path,
    lock_overrides: dict[str, Path] | None = None,
    source_patch: Path | None = None,
) -> dict[str, Any]:
    source_root = source_root.resolve(strict=True)
    ew_wire_root = ew_wire_root.resolve(strict=True)
    if destination.exists() or destination.is_symlink():
        raise ValueError(f"Refusing to overwrite Antoine source snapshot: {destination}")
    before = manifest(source_root, ew_wire_root, lock_overrides)
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{destination.name}.", dir=destination.parent))
    try:
        for source, relative in source_files(source_root, ew_wire_root, lock_overrides):
            target = staging / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
            shutil.copystat(source, target, follow_symlinks=False)
        copied = [
            {
                "path": item["path"],
                "sizeBytes": (staging / item["path"]).stat().st_size,
                "sha256": sha256(staging / item["path"]),
            }
            for item in before
        ]
        after = manifest(source_root, ew_wire_root, lock_overrides)
        if before != copied or before != after:
            raise RuntimeError(
                "Antoine build inputs changed while the run-owned snapshot was being created"
            )
        patch_receipt = None
        if source_patch is not None:
            if source_patch.is_symlink() or not source_patch.is_file():
                raise ValueError("Source patch must be a regular non-symlink file")
            patch_bytes = source_patch.read_bytes()
            # Only modifications of existing Antoine source files are allowed.
            # Reject absolute paths, traversal, new files, and non-patch input.
            paths = []
            for line in patch_bytes.decode("utf-8").splitlines():
                if line.startswith(("--- ", "+++ ")):
                    value = line[4:].split("\t", 1)[0]
                    prefix = "a/" if line.startswith("--- ") else "b/"
                    if not value.startswith(prefix):
                        raise ValueError("Source patch must modify existing relative paths")
                    relative = Path(value[2:])
                    if relative.is_absolute() or ".." in relative.parts:
                        raise ValueError("Unsafe source patch path")
                    if str(Path("antoine") / relative) not in {item["path"] for item in before}:
                        raise ValueError("Source patch target is not a snapshotted source file")
                    paths.append(value[2:])
            if not paths or len(paths) % 2 or paths[::2] != paths[1::2]:
                raise ValueError("Source patch must contain paired existing file paths")
            command = ["patch", "--batch", "--fuzz=0", "-V", "never", "-p1"]

            patch_lines = patch_bytes.decode("utf-8").splitlines(keepends=True)
            section_starts = [
                index for index, line in enumerate(patch_lines) if line.startswith("diff --git ")
            ]
            if not section_starts:
                section_starts = [0]
            section_starts.append(len(patch_lines))
            hunks: list[bytes] = []
            for section_index in range(len(section_starts) - 1):
                section = patch_lines[
                    section_starts[section_index] : section_starts[section_index + 1]
                ]
                hunk_starts = [
                    index for index, line in enumerate(section) if line.startswith("@@ ")
                ]
                if not hunk_starts:
                    continue
                header = "".join(section[: hunk_starts[0]])
                for hunk_index, hunk_start in enumerate(hunk_starts):
                    hunk_end = (
                        hunk_starts[hunk_index + 1]
                        if hunk_index + 1 < len(hunk_starts)
                        else len(section)
                    )
                    hunks.append((header + "".join(section[hunk_start:hunk_end])).encode("utf-8"))
            if not hunks:
                raise ValueError("Source patch contains no unified diff hunks")

            def check_patch(hunk: bytes, reverse: bool) -> subprocess.CompletedProcess[bytes]:
                return subprocess.run(
                    command + (["--reverse"] if reverse else ["--forward"]) + ["--dry-run"],
                    input=hunk,
                    cwd=staging / "antoine",
                    capture_output=True,
                    timeout=10,
                )

            def patch_check_succeeded(result: subprocess.CompletedProcess[bytes]) -> bool:
                output = (result.stdout + result.stderr).decode("utf-8", errors="replace").lower()
                return result.returncode == 0 and "ignored" not in output

            patch_states = [
                (
                    patch_check_succeeded(check_patch(hunk, False)),
                    patch_check_succeeded(check_patch(hunk, True)),
                )
                for hunk in hunks
            ]
            semantic_states = [
                (
                    forward,
                    reverse,
                    patch_changes_present([hunk], staging / "antoine")
                    or patch_hunk_superseded_by_upstream(hunk, staging / "antoine"),
                )
                for hunk, (forward, reverse) in zip(hunks, patch_states, strict=True)
            ]
            unmatched = [
                index
                for index, (forward, reverse, semantic) in enumerate(semantic_states)
                if not forward and not reverse and not semantic
            ]
            if unmatched:
                details = []
                for index in unmatched:
                    lines = hunks[index].decode("utf-8", errors="replace").splitlines()
                    target = next(
                        (line[6:] for line in lines if line.startswith("+++ b/")), "unknown"
                    )
                    location = next(
                        (line for line in lines if line.startswith("@@ ")), "unknown hunk"
                    )
                    details.append(f"{target} {location}")
                raise ValueError(
                    "Source patch does not match this Antoine source revision: "
                    + "; ".join(details)
                )
            applied_any = False
            for hunk, (forward, reverse, semantic) in zip(hunks, semantic_states, strict=True):
                if forward:
                    applied = subprocess.run(
                        command,
                        input=hunk,
                        cwd=staging / "antoine",
                        capture_output=True,
                        timeout=10,
                    )
                    if applied.returncode:
                        details = (
                            (applied.stdout + applied.stderr)
                            .decode("utf-8", errors="replace")
                            .strip()
                        )
                        raise ValueError(f"Source patch application failed: {details}")
                    applied_any = True
                elif reverse or semantic:
                    continue
                else:
                    raise ValueError("Source patch does not match this Antoine source revision")
            patch_status = "applied" if applied_any else "already-present"
            original_paths = {item["path"] for item in before}
            # Apple's patch creates .orig files even with versioned backups
            # disabled, and .rej files for already-applied hunks when a
            # combined research patch contains both existing and new changes.
            # Remove only those patch-generated siblings for declared targets.
            for path in set(paths):
                for suffix in (".orig", ".rej", ".rej.orig"):
                    backup = Path("antoine") / f"{path}{suffix}"
                    if backup.as_posix() not in original_paths:
                        (staging / backup).unlink(missing_ok=True)
            actual_paths = {
                relative.as_posix()
                for _, relative in source_files(staging / "antoine", staging / "ew-wire-contract")
            }
            if actual_paths != original_paths:
                raise ValueError(
                    "Source patch produced unrecorded source files: "
                    f"extra={sorted(actual_paths - original_paths)}, "
                    f"missing={sorted(original_paths - actual_paths)}"
                )
            copied = [
                {
                    "path": item["path"],
                    "sizeBytes": (staging / item["path"]).stat().st_size,
                    "sha256": sha256(staging / item["path"]),
                }
                for item in before
            ]
            patch_receipt = {
                "sha256": hashlib.sha256(patch_bytes).hexdigest(),
                "status": patch_status,
                "paths": sorted(set(paths)),
            }
        receipt: dict[str, Any] = {
            "contract": "damn-vulnerable-drone.antoine-source-snapshot",
            "version": "1.1.0",
            "sourceRootLabel": source_root.name,
            "ewWireRootLabel": ew_wire_root.name,
            "sourceManifestSha256": manifest_digest(before),
            "snapshotManifestSha256": manifest_digest(copied),
            "fileCount": len(copied),
            "status": "verified",
            "files": copied,
            "sourcePatch": patch_receipt,
        }
        (staging / "antoine-source-snapshot.json").write_text(
            json.dumps(receipt, indent=2) + "\n", encoding="utf-8"
        )
        os.replace(staging, destination)
        return receipt
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("antoine_source", type=Path)
    parser.add_argument("ew_wire_source", type=Path)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--sitl-lockfile", type=Path)
    parser.add_argument("--boundary-lockfile", type=Path)
    parser.add_argument("--source-patch", type=Path)
    args = parser.parse_args()
    if bool(args.sitl_lockfile) != bool(args.boundary_lockfile):
        parser.error("--sitl-lockfile and --boundary-lockfile must be supplied together")
    lock_overrides = None
    if args.sitl_lockfile and args.boundary_lockfile:
        lock_overrides = {
            "antoine/sitl/antoine-sitl/Cargo.lock": args.sitl_lockfile,
            "antoine/runtime/antoine-boundary/Cargo.lock": args.boundary_lockfile,
        }
    try:
        receipt = create_snapshot(
            args.antoine_source,
            args.ew_wire_source,
            args.destination,
            lock_overrides,
            args.source_patch,
        )
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        print(f"Antoine source snapshot failed: {error}", file=sys.stderr)
        return 2
    print(
        json.dumps({key: receipt[key] for key in ("status", "snapshotManifestSha256", "fileCount")})
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
