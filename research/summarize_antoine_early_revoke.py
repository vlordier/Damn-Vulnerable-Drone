#!/usr/bin/env python3
"""Join a local signed early revoke to its controller, actuator, and pose evidence."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from build_evidence_manifest import build, write_manifest
from dvd_research.strict_json import loads as strict_json_loads
from evidence_contracts import ordered_timestamps, read_json_object, run_identity_matches


def read_actuator_audit(path: Path) -> tuple[list[dict], bool]:
    """Read actuator JSONL while making any malformed or ambiguous row disqualifying."""
    rows = []
    valid = True
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return rows, False
    for line in lines:
        if not line.strip():
            continue
        try:
            row = strict_json_loads(line)
        except ValueError:
            valid = False
            continue
        if not isinstance(row, dict):
            valid = False
            continue
        rows.append(row)
    return rows, valid


def read(path: Path):
    return read_json_object(path)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--launcher-exit", type=int, required=True)
    args = parser.parse_args()
    run_dir = args.run_dir
    flight = read(run_dir / "gz-flight-proof-report.json")
    grant = flight.get("localGrant") if isinstance(flight.get("localGrant"), dict) else {}
    run_id = run_dir.name
    authority_log = (
        (run_dir / "logs" / "local-authority.log").read_text(encoding="utf-8", errors="replace")
        if (run_dir / "logs" / "local-authority.log").is_file()
        else ""
    )
    controller_log = (
        (run_dir / "logs" / "antoine-controller.log").read_text(encoding="utf-8", errors="replace")
        if (run_dir / "logs" / "antoine-controller.log").is_file()
        else ""
    )
    revoke_re = re.compile(
        r"revoke-published run_id=(\S+) grant_sequence=(\d+) published_at_unix_ms=(\d+) trigger=timed"
    )
    accepted_re = re.compile(
        r"ANTOINE_GZ_GRANT_REVOKED .*?run_id=\"?(\S+?)\"? grant_sequence=\"?(\d+)\"? accepted_at_unix_ms=(\d+)"
    )
    published_matches = list(revoke_re.finditer(authority_log))
    accepted_matches = list(accepted_re.finditer(controller_log))
    published = published_matches[0] if published_matches else None
    accepted = accepted_matches[0] if accepted_matches else None
    audit_path = run_dir / "applier-neutral" / "actuator-output.jsonl"
    audit_rows, audit_jsonl_valid = read_actuator_audit(audit_path)
    published_ms = int(published.group(3)) if published else None
    accepted_ms = int(accepted.group(3)) if accepted else None
    neutral_rows = [
        row
        for row in audit_rows
        if row.get("contract") == "furia.gzsitl.actuator-output-audit"
        and row.get("stage") == "explicit-stop"
        and row.get("actuatorKind") == "motors"
        and row.get("vehicleId") == "drone-1"
        and row.get("authority") == "safe-stop"
        and row.get("neutral") is True
        and row.get("runId") == run_id
        and type(row.get("recordedAtUnixMs")) is int
        and published_ms is not None
        and row["recordedAtUnixMs"] >= published_ms
    ]
    neutral = neutral_rows[0] if len(neutral_rows) == 1 else None
    measured = flight.get("measured") if isinstance(flight.get("measured"), dict) else {}
    gates = {
        "flightReportAvailable": (run_dir / "gz-flight-proof-report.json").is_file(),
        "runIdentityMatches": run_identity_matches(
            run_dir,
            grant.get("runId"),
            published.group(1) if published else None,
            accepted.group(1).rstrip('"') if accepted else None,
        ),
        "grantSequenceMatches": bool(
            published and accepted and published.group(2) == accepted.group(2)
        ),
        "oneTimedRevokePublished": len(published_matches) == 1,
        "oneControllerAcceptanceObserved": len(accepted_matches) == 1,
        "neutralActuatorOutputAfterRevoke": neutral is not None,
        "actuatorAuditJsonlValid": audit_jsonl_valid,
        "oneNeutralStopObserved": len(neutral_rows) == 1,
        "independentPoseTimelineAvailable": flight.get("poseTimelinePassed") is True,
        "actuatorNeutralizationPassed": flight.get("actuatorNeutralizationPassed") is True,
        "revokePreemptedMission": measured.get("allLegsReached") is False,
    }
    stopped_ms = neutral.get("recordedAtUnixMs") if neutral else None
    event_order_ok = ordered_timestamps(published_ms, accepted_ms, stopped_ms)
    gates["eventOrderValid"] = event_order_ok
    latency = (
        stopped_ms - published_ms if stopped_ms is not None and published_ms is not None else None
    )
    result = {
        "contract": "damn-vulnerable-drone.antoine-early-revoke-result",
        "version": "1.0.0",
        "scenario": "early-authority-revoke-antoine",
        "runId": run_id,
        "scope": "one timed signed revoke from the run-owned local demo authority during a finite Gazebo flight",
        "gates": gates,
        "revokePublishedAtUnixMs": published_ms,
        "launcherExitCode": args.launcher_exit,
        "revokeAcceptedAtUnixMs": accepted_ms,
        "eventOrderValid": event_order_ok,
        "neutralActuatorAtUnixMs": stopped_ms,
        "revokeToNeutralMs": latency,
        "grantSequence": int(published.group(2)) if published else None,
        "localFlightPassed": flight.get("localFlightPassed"),
        "allLegsReached": measured.get("allLegsReached"),
        "interpretation": "A run-matched signed revoke interrupted the authorized waypoint sequence and was followed by same-run neutral actuator output with independent pose evidence. This is a local stop-latency observation; it does not establish landing, collision avoidance, or hardware safety."
        if all(gates.values())
        else "Early revoke qualification is inconclusive; in particular, a revoke after all mission legs were completed is not an early-interruption result. No landing or hardware-safety claim is made.",
    }
    out = run_dir / "research-early-revoke.json"
    out.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    write_manifest(run_dir, build(run_dir))
    print(out)
    return 0 if all(gates.values()) else 2


if __name__ == "__main__":
    raise SystemExit(main())
