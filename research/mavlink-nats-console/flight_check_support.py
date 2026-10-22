"""Small runtime gates shared by the MAVLink acceptance commands."""

from __future__ import annotations

import math


def require_check(condition: object, message: object) -> None:
    """Raise on failed acceptance conditions even under Python -O."""
    if not condition:
        raise AssertionError(message)


def finite_json_number(value: object) -> bool:
    if type(value) not in {int, float}:
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False
