"""Parsers for run-bound controller diagnostics that do not qualify flight outcomes."""

from __future__ import annotations

import re
from typing import Any


def marker(line: str, name: str) -> dict[str, str] | None:
    match = re.search(rf"{re.escape(name)}\s+(.*)$", line)
    if not match:
        return None
    return dict(re.findall(r"([A-Za-z][A-Za-z0-9_]*)=([^\s]+)", match.group(1)))


def state_coalescing_diagnostic(lines: list[str] | None, callsign: str) -> dict[str, Any]:
    """Summarize backlog drops only when the run confirms instrumentation was enabled."""
    if lines is None:
        return {
            "status": "unavailable",
            "reason": "controller-log-unavailable",
            "callsign": callsign,
            "eventCount": 0,
            "droppedSupersededMessages": None,
            "events": [],
            "qualifiesFlightOutcome": False,
        }

    enabled = [
        value
        for line in lines
        if (value := marker(line, "ANTOINE_GZ_STATE_COALESCING_ENABLED"))
        and value.get("callsign") == callsign
    ]
    raw_events = [
        value
        for line in lines
        if (value := marker(line, "ANTOINE_GZ_STATE_COALESCED"))
        and value.get("callsign") == callsign
    ]
    events: list[dict[str, int]] = []
    invalid = 0
    for value in raw_events:
        try:
            dropped = int(value["dropped_superseded"])
        except (KeyError, ValueError):
            invalid += 1
            continue
        if dropped <= 0:
            invalid += 1
            continue
        events.append({"droppedSupersededMessages": dropped})

    if len(enabled) > 1 or (raw_events and not enabled):
        status = "invalid"
    elif invalid:
        status = "invalid"
    elif not enabled:
        status = "unavailable"
    else:
        status = "measured"
    result: dict[str, Any] = {
        "status": status,
        "callsign": callsign,
        "enabledMarkerCount": len(enabled),
        "eventCount": len(events),
        "droppedSupersededMessages": (
            sum(item["droppedSupersededMessages"] for item in events)
            if status == "measured"
            else None
        ),
        "events": events,
        "qualifiesFlightOutcome": False,
    }
    if status == "unavailable":
        result["reason"] = "coalescing-instrumentation-not-confirmed"
    return result
