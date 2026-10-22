"""Small runtime gates shared by the MAVLink acceptance commands."""

from __future__ import annotations


def require_check(condition: object, message: object) -> None:
    """Raise on failed acceptance conditions even under Python -O."""
    if not condition:
        raise AssertionError(message)
