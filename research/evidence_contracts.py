"""Shared strict checks for run-bound Antoine evidence contracts."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def run_identity_matches(run_dir: Path, *run_ids: Any) -> bool:
    """Require every referenced artifact to identify the exact containing run."""
    return bool(run_ids) and all(
        isinstance(value, str) and value == run_dir.name for value in run_ids
    )


def exact_count(report: dict[str, Any], field: str, expected: int) -> bool:
    """Reject bools, strings, absent values, and any cardinality other than expected."""
    value = report.get(field)
    return type(value) is int and value == expected


def ordered_timestamps(*values: Any) -> bool:
    """Require integer Unix-millisecond timestamps in causal nondecreasing order."""
    return all(type(value) is int for value in values) and list(values) == sorted(values)


def read_json_object(path: Path) -> dict[str, Any]:
    """Return an empty object on absent/malformed evidence so gates fail closed."""
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}
