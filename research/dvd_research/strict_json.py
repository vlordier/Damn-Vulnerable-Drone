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


def raw_decode(data: str, idx: int = 0) -> tuple[Any, int]:
    """Decode one JSON value from a larger stream with the same strict rules."""
    decoder = json.JSONDecoder(object_pairs_hook=_unique_object, parse_constant=_reject_constant)
    return decoder.raw_decode(data, idx)


def skip_json_object(data: str, start: int) -> int:
    """Skip one balanced object when strict decoding rejects its contents."""
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(data)):
        char = data[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return index + 1
    return len(data)
