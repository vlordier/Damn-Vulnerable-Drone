#!/usr/bin/env python3
"""Refuse a swarm flight when recent host load leaves too little CPU headroom."""

from __future__ import annotations

import argparse
import math
import os
import sys
from collections.abc import Sequence
from typing import Any


def measure_load(
    load_averages: Sequence[float], logical_cpu_count: int, max_load_per_cpu: float
) -> dict[str, Any]:
    if type(logical_cpu_count) is not int or logical_cpu_count < 1:
        raise ValueError("logical CPU count must be positive")
    if (
        isinstance(max_load_per_cpu, bool)
        or not isinstance(max_load_per_cpu, (int, float))
        or not math.isfinite(max_load_per_cpu)
        or max_load_per_cpu <= 0
    ):
        raise ValueError("maximum load per CPU must be finite and positive")
    if len(load_averages) < 2 or any(
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value < 0
        for value in load_averages[:2]
    ):
        raise ValueError("one-minute and five-minute load averages must be finite and non-negative")

    maximum = max(load_averages[0], load_averages[1])
    limit = logical_cpu_count * max_load_per_cpu
    return {
        "status": "quiet" if maximum <= limit else "busy",
        "loadAverage1m": load_averages[0],
        "loadAverage5m": load_averages[1],
        "logicalCpuCount": logical_cpu_count,
        "maximumLoadPerCpu": max_load_per_cpu,
        "maximumAllowedLoad": limit,
        "measuredMaximumLoad": maximum,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    try:
        default_load_cap = float(os.environ.get("ANTOINE_SWARM_MAX_LOAD_PER_CPU", "0.75"))
    except ValueError:
        parser.error("ANTOINE_SWARM_MAX_LOAD_PER_CPU must be numeric")
    parser.add_argument(
        "--max-load-per-cpu",
        type=float,
        default=default_load_cap,
    )
    args = parser.parse_args()
    try:
        cpu_count = os.cpu_count()
        if cpu_count is None:
            raise ValueError("logical CPU count is unavailable")
        result = measure_load(os.getloadavg(), cpu_count, args.max_load_per_cpu)
    except (OSError, ValueError) as error:
        print(f"Swarm host-load preflight unavailable: {error}", file=sys.stderr)
        return 2

    print(
        "Host load preflight: "
        f"1m={result['loadAverage1m']:.2f}, 5m={result['loadAverage5m']:.2f}, "
        f"CPUs={result['logicalCpuCount']}, limit={result['maximumAllowedLoad']:.2f} "
        f"({result['maximumLoadPerCpu']:.2f} per CPU)."
    )
    if result["status"] != "quiet":
        print("Host load is too high for a matched swarm experiment.", file=sys.stderr)
        print(
            "Wait for unrelated work to finish or set ANTOINE_SWARM_MAX_LOAD_PER_CPU explicitly.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
