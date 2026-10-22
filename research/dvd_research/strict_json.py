"""Strict JSON decoding for NATS records and independent run evidence."""

from __future__ import annotations

import json
from typing import Any


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise json.JSONDecodeError(f"duplicate object key {key!r}", "", 0)
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise json.JSONDecodeError(f"non-standard JSON constant {value}", "", 0)


def loads(data: str | bytes | bytearray) -> Any:
    """Decode JSON while rejecting duplicate keys and NaN/Infinity tokens."""
    return json.loads(data, object_pairs_hook=_unique_object, parse_constant=_reject_constant)
