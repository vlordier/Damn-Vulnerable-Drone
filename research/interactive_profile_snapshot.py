"""Validation shared by interactive-profile snapshot receipts and pins."""

from __future__ import annotations

import re
from pathlib import PurePosixPath
from typing import Any

SNAPSHOT_RECORD_FIELDS = frozenset({"path", "receiptSha256", "snapshotManifestSha256", "fileCount"})
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


def snapshot_record_error(value: Any) -> str | None:
    """Return why a snapshot pin record is malformed, or None when complete."""
    if not isinstance(value, dict) or set(value) != SNAPSHOT_RECORD_FIELDS:
        return "snapshot fields are missing or unsupported"

    path = value.get("path")
    if not isinstance(path, str) or not path or "\\" in path or "\0" in path:
        return "snapshot path must be a canonical relative POSIX path"
    parsed_path = PurePosixPath(path)
    if (
        parsed_path.is_absolute()
        or parsed_path.as_posix() != path
        or not parsed_path.parts
        or any(part in {"", ".", ".."} for part in parsed_path.parts)
    ):
        return "snapshot path must be a canonical relative POSIX path"

    for field in ("receiptSha256", "snapshotManifestSha256"):
        digest = value.get(field)
        if not isinstance(digest, str) or _SHA256.fullmatch(digest) is None:
            return f"{field} must be a lowercase SHA-256 digest"

    file_count = value.get("fileCount")
    if type(file_count) is not int or file_count <= 0:
        return "fileCount must be a positive integer"
    return None
