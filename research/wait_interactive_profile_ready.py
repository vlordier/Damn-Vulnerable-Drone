#!/usr/bin/env python3
"""Wait until a run-owned Antoine/Gazebo profile has end-to-end evidence."""

from __future__ import annotations

import argparse
import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import interactive_profile


def wait_until_recorded(
    receipt: Path,
    timeout_seconds: float = 60.0,
    poll_interval_seconds: float = 1.0,
    *,
    inspect: Callable[[Path], dict[str, Any]] = interactive_profile.inspect,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    if not 0 < timeout_seconds <= 300:
        raise ValueError("readiness timeout must be greater than 0 and at most 300 seconds")
    if not 0 < poll_interval_seconds <= 10:
        raise ValueError("readiness poll interval must be greater than 0 and at most 10 seconds")

    deadline = monotonic() + timeout_seconds
    last_status: dict[str, Any] = {}
    while True:
        last_status = inspect(receipt)
        if last_status.get("status") == "recorded" and last_status.get("armed") is False:
            return last_status
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise TimeoutError(
                "profile did not reach recorded/disarmed readiness before timeout; "
                f"last status: {json.dumps(last_status, sort_keys=True)}"
            )
        sleep(min(poll_interval_seconds, remaining))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("receipt", type=Path)
    parser.add_argument("--timeout-seconds", type=float, default=60.0)
    args = parser.parse_args()
    try:
        result = wait_until_recorded(args.receipt, args.timeout_seconds)
    except (OSError, ValueError, TypeError, TimeoutError) as error:
        print(f"Profile readiness failed: {error}")
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
