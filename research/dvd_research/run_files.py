"""Read and atomically write artifacts contained by one research run."""

from __future__ import annotations

import json
import os
import stat
import tempfile
from pathlib import Path
from typing import Any

from dvd_research.evidence_contract import contains_symlink_component


def _run_root(run_dir: Path) -> Path | None:
    run_dir = Path(run_dir)
    if run_dir.is_symlink():
        raise ValueError("run directory is a symlink")
    try:
        root = run_dir.resolve(strict=True)
    except FileNotFoundError:
        return None
    if not root.is_dir():
        raise ValueError("run path is not a directory")
    return root


def _relative_path(relative_path: Path) -> Path:
    relative_path = Path(relative_path)
    if relative_path.is_absolute() or ".." in relative_path.parts or not relative_path.parts:
        raise ValueError("run artifact path must be a non-empty relative path")
    return relative_path


def resolve_run_file(run_dir: Path, relative_path: Path) -> Path | None:
    """Resolve an existing regular file only when every path component is run-owned."""
    relative_path = _relative_path(relative_path)
    root = _run_root(run_dir)
    if root is None:
        return None
    if contains_symlink_component(root, relative_path):
        raise ValueError("run artifact path contains a symlink")
    path = root / relative_path
    try:
        resolved = path.resolve(strict=True)
    except FileNotFoundError:
        return None
    try:
        resolved.relative_to(root)
    except ValueError as error:
        raise ValueError("run artifact escapes its directory") from error
    if not stat.S_ISREG(path.stat().st_mode):
        raise ValueError("run artifact is not a regular file")
    return path


def read_run_text(
    run_dir: Path,
    relative_path: Path,
    *,
    encoding: str = "utf-8",
    errors: str = "strict",
) -> str | None:
    path = resolve_run_file(run_dir, relative_path)
    if path is None:
        return None
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(descriptor, "r", encoding=encoding, errors=errors) as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError("run artifact is not a regular file")
        return stream.read()


def read_run_tail_text(
    run_dir: Path,
    relative_path: Path,
    max_bytes: int,
    *,
    encoding: str = "utf-8",
    errors: str = "replace",
) -> str | None:
    """Read a bounded tail, dropping a partial first record after seeking."""
    if type(max_bytes) is not int or max_bytes <= 0:
        raise ValueError("run artifact tail size must be a positive integer")
    path = resolve_run_file(run_dir, relative_path)
    if path is None:
        return None
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(descriptor, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError("run artifact is not a regular file")
        size = os.fstat(stream.fileno()).st_size
        stream.seek(max(0, size - max_bytes))
        if stream.tell():
            stream.readline()
        return stream.read().decode(encoding, errors=errors)


def write_run_text(run_dir: Path, relative_path: Path, content: str) -> Path:
    """Atomically write a run artifact without following a linked destination."""
    relative_path = _relative_path(relative_path)
    root = _run_root(run_dir)
    if root is None:
        raise ValueError("run directory does not exist")
    parent_relative = relative_path.parent
    if parent_relative != Path(".") and contains_symlink_component(root, parent_relative):
        raise ValueError("run artifact parent path contains a symlink")
    parent = root / parent_relative
    if not parent.is_dir():
        raise ValueError("run artifact parent directory is unavailable")
    destination = root / relative_path
    if destination.is_symlink():
        raise ValueError("run artifact destination is a symlink")

    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return destination


def write_run_json(run_dir: Path, relative_path: Path, value: Any) -> Path:
    content = json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n"
    return write_run_text(run_dir, relative_path, content)
