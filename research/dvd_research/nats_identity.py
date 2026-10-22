"""Validation for vehicle identities embedded in NATS subject tokens."""

from __future__ import annotations

from typing import Any

MAX_CALLSIGN_LENGTH = 64


def valid_callsign(value: Any) -> bool:
    """Match the MAVLink bridge's single-token ASCII callsign contract."""
    return (
        isinstance(value, str)
        and 1 <= len(value) <= MAX_CALLSIGN_LENGTH
        and all(
            character.isascii() and (character.isalnum() or character in "-_")
            for character in value
        )
    )
