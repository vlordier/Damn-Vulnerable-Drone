#!/usr/bin/env python3
"""Send a capped wrong-callsign setpoint burst to one run-owned NATS subject."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import subprocess
import time
from pathlib import Path
from urllib.parse import urlsplit

from dvd_research.run_files import read_run_text, write_run_json
from dvd_research.run_nats import validate_run_nats_endpoint
from dvd_research.strict_json import loads as strict_json_loads
from swarm_cross_vehicle_probe import (
    pose_window_at_target,
    read_one_json,
    target_plan,
    validate_run_evidence_paths,
    vehicle_effect_snapshot,
)

MAX_RATE_HZ = 20
MAX_DURATION_SECONDS = 5.0
MAX_MESSAGES = 100
SEQUENCE_HEADROOM = 100


def validate_server(server: str) -> None:
    parsed = urlsplit(server)
    if (
        parsed.scheme != "nats"
        or parsed.hostname != "127.0.0.1"
        or parsed.port is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("refusing any NATS endpoint except explicit IPv4 loopback")


def validate_limits(rate_hz: int, duration: float, maximum: int) -> int:
    if type(rate_hz) is not int or not 1 <= rate_hz <= MAX_RATE_HZ:
        raise ValueError(f"rate must be 1..{MAX_RATE_HZ} Hz")
    if not math.isfinite(duration) or not 0.25 <= duration <= MAX_DURATION_SECONDS:
        raise ValueError(f"duration must be 0.25..{MAX_DURATION_SECONDS:g} seconds")
    if type(maximum) is not int or not 1 <= maximum <= MAX_MESSAGES:
        raise ValueError(f"message cap must be 1..{MAX_MESSAGES}")
    count = min(maximum, max(1, math.ceil(rate_hz * duration)))
    if count > MAX_MESSAGES:
        raise ValueError("computed message count exceeds hard cap")
    return count


def mutate(payload: dict, source: str, target: str, sequence: int) -> dict:
    if (
        payload.get("contract") != "furia.gzsitl.mission-setpoint"
        or payload.get("version") != "2.1.0"
        or payload.get("vehicleId") != target
        or type(payload.get("sequence")) is not int
    ):
        raise ValueError("captured message does not match the live target setpoint contract")
    result = dict(payload)
    result["vehicleId"] = source
    result["sequence"] = sequence
    return result


def capture_setpoint(server: str, subject: str) -> dict:
    completed = subprocess.run(
        ["nats", "--no-context", "--server", server, "sub", "--count=1", "--raw", subject],
        check=True,
        capture_output=True,
        text=True,
        timeout=8,
    )
    return read_one_json(completed.stdout)


def rejection_sequences(
    lines: list[str],
    target: str,
    source: str,
    sequences: list[int],
    payload_hashes: dict[int, str] | None = None,
) -> set[int]:
    found = set()
    for line in lines:
        if f"ANTOINE_GZ_SETPOINT_REJECTED callsign={target} " not in line:
            continue
        tokens = set(line.split())
        if f"vehicle_id={source}" not in tokens:
            continue
        for sequence in sequences:
            hash_matches = (
                payload_hashes is None or f"payload_sha256={payload_hashes.get(sequence)}" in tokens
            )
            if f"sequence={sequence}" in tokens and hash_matches:
                found.add(sequence)
    return found


def accepted_sequences(lines: list[str], target: str, payload_hashes: dict[int, str]) -> set[int]:
    """Match accepted controller receipts to exact injected payload hashes."""
    found = set()
    for line in lines:
        if f"ANTOINE_GZ_SETPOINT_RECEIVED callsign={target} " not in line:
            continue
        tokens = set(line.split())
        for sequence, payload_hash in payload_hashes.items():
            if f"sequence={sequence}" in tokens and f"payload_sha256={payload_hash}" in tokens:
                found.add(sequence)
    return found


async def publish_burst(
    server: str, subject: str, payloads: list[dict], rate_hz: int
) -> tuple[int, int]:
    import nats

    client = await nats.connect(server, connect_timeout=2, max_reconnect_attempts=0)
    first_ns = time.time_ns()
    monotonic_start = time.monotonic()
    try:
        for index, payload in enumerate(payloads):
            deadline = monotonic_start + index / rate_hz
            delay = deadline - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            await client.publish(subject, json.dumps(payload, separators=(",", ":")).encode())
        await client.flush(timeout=2)
        return first_ns, time.time_ns()
    finally:
        await client.close()


async def run(args: argparse.Namespace) -> dict:
    validate_server(args.server)
    count = validate_limits(args.rate_hz, args.duration, args.max_messages)
    if args.source == args.target:
        raise ValueError("source and target callsigns must differ")
    validate_run_evidence_paths(args.run_dir, args.target)
    validate_run_nats_endpoint(
        args.server, args.run_dir, time.monotonic() + args.wait_timeout_seconds
    )
    run_dir = args.run_dir.resolve(strict=True)
    model, target_position = target_plan(run_dir, args.target)
    config_text = read_run_text(run_dir, Path("research-swarm-config.json"))
    if config_text is None:
        raise ValueError("run-owned swarm config is unavailable")
    config = strict_json_loads(config_text)
    models = {
        item["callsign"]: item["model"]
        for item in config.get("vehicles", [])
        if isinstance(item, dict)
        and isinstance(item.get("callsign"), str)
        and isinstance(item.get("model"), str)
    }
    if args.source not in models or args.target not in models or len(models) < 2:
        raise ValueError("run config must bind source and target callsigns to distinct models")
    pose_path = run_dir / "gazebo-pose.jsonl"
    controller_log = Path("logs") / f"antoine-controller-{args.target}.log"
    deadline = time.monotonic() + args.wait_timeout_seconds
    pose_window = None
    while time.monotonic() < deadline:
        pose_window = pose_window_at_target(pose_path, model, target_position)
        if pose_window:
            break
        await asyncio.sleep(0.2)
    if not pose_window:
        raise TimeoutError("independent Gazebo pose did not hold the target for the required dwell")

    before_effect = vehicle_effect_snapshot(run_dir, models)
    before_log = read_run_text(run_dir, controller_log, errors="replace") or ""
    subject = f"furia.gzsitl.setpoint.{args.target}"
    original = capture_setpoint(args.server, subject)
    sequence = original["sequence"] + SEQUENCE_HEADROOM
    payloads = [
        mutate(original, args.source, args.target, sequence + index) for index in range(count)
    ]
    hashes = [
        hashlib.sha256(json.dumps(item, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        for item in payloads
    ]
    expected = [item["sequence"] for item in payloads]
    payload_hashes = dict(zip(expected, hashes, strict=True))
    started_ns, published_ns = await publish_burst(args.server, subject, payloads, args.rate_hz)
    observation_deadline = time.monotonic() + 5
    decision_lines: list[str] = []
    rejected: set[int] = set()
    accepted = set()
    while time.monotonic() < observation_deadline and len(rejected) < count:
        after_text = read_run_text(run_dir, controller_log, errors="replace") or ""
        decision_lines = after_text[len(before_log) :].splitlines()
        rejected = rejection_sequences(
            decision_lines, args.target, args.source, expected, payload_hashes
        )
        accepted = accepted_sequences(decision_lines, args.target, payload_hashes)
        if len(rejected) < count:
            await asyncio.sleep(0.05)
    after_effect = vehicle_effect_snapshot(run_dir, models)
    now_ns = time.time_ns()
    missing = sorted(set(expected) - rejected)
    status = "failed" if accepted else "passed" if len(rejected) == count else "inconclusive"
    return {
        "contract": "damn-vulnerable-drone.antoine-setpoint-rate-probe",
        "version": "1.0.0",
        "runId": run_dir.name,
        "status": status,
        "sourceVehicle": args.source,
        "targetVehicle": args.target,
        "subject": subject,
        "mutation": "wrong-callsign with unique monotonic probe sequences",
        "rateHz": args.rate_hz,
        "durationSecondsCap": args.duration,
        "messageCap": args.max_messages,
        "attemptedMessages": count,
        "publishedMessages": count,
        "rejectedMessages": len(rejected),
        "missingRejectionSequences": missing,
        "acceptedCandidateSequences": sorted(accepted),
        "sequenceRange": [expected[0], expected[-1]],
        "sequenceHeadroom": SEQUENCE_HEADROOM,
        "publicationStartedAtUnixNs": started_ns,
        "publicationCompletedAtUnixNs": published_ns,
        "decisionObservedAtUnixNs": now_ns,
        "controllerDecisionUpperBoundMs": round((now_ns - started_ns) / 1e6, 3),
        "uniqueMutatedPayloadSha256": len(set(hashes)),
        "injectedPayloadSha256BySequence": {
            str(sequence): digest for sequence, digest in payload_hashes.items()
        },
        "preAttackPoseWindow": pose_window,
        "independentEffectBefore": before_effect,
        "independentEffectAfter": after_effect,
        "controllerDecisionEvidence": [
            line
            for line in decision_lines
            if f"ANTOINE_GZ_SETPOINT_REJECTED callsign={args.target}" in line
            or f"ANTOINE_GZ_SETPOINT_RECEIVED callsign={args.target}" in line
        ][-MAX_MESSAGES:],
        "scope": "run-owned IPv4 loopback NATS only; wrong-callsign setpoint envelopes; no broker/network packet disruption",
        "limits": "This is a bounded parser/consumer load sample, not a general DDoS test or a shared-broker rate-limit finding. Flight and actuator safety qualification remains in the parent run summary.",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server", required=True)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--source", default="drone-1")
    parser.add_argument("--target", default="drone-2")
    parser.add_argument("--rate-hz", type=int, default=10)
    parser.add_argument("--duration", type=float, default=2.0)
    parser.add_argument("--max-messages", type=int, default=20)
    parser.add_argument("--wait-timeout-seconds", type=float, default=90.0)
    args = parser.parse_args()
    if not 1 <= args.wait_timeout_seconds <= 180:
        parser.error("wait timeout must be 1..180 seconds")
    try:
        report = asyncio.run(run(args))
    except (OSError, ValueError, TimeoutError, subprocess.SubprocessError, KeyError) as error:
        report = {
            "contract": "damn-vulnerable-drone.antoine-setpoint-rate-probe",
            "version": "1.0.0",
            "runId": args.run_dir.name,
            "status": "inconclusive",
            "error": str(error),
            "publishedMessages": 0,
            "limits": "No message is sent unless the explicit loopback broker, run config, and live pose gates pass.",
        }
    args.run_dir.mkdir(parents=True, exist_ok=True)
    output = write_run_json(args.run_dir, Path("research-swarm-rate-disruption.json"), report)
    fault = {
        "contract": "damn-vulnerable-drone.antoine-swarm-fault",
        "version": "1.0.0",
        "runId": report.get("runId"),
        "fault": "command-rate",
        "status": report.get("status", "inconclusive"),
        "mechanism": "bounded wrong-callsign setpoint request burst on one run-owned loopback subject",
        "startedAtUnixNs": report.get("publicationStartedAtUnixNs"),
        "completedAtUnixNs": report.get("decisionObservedAtUnixNs"),
        "publishedMessages": report.get("publishedMessages", 0),
        "rejectedMessages": report.get("rejectedMessages", 0),
        "limits": report.get("limits"),
        "evidence": "research-swarm-rate-disruption.json",
    }
    write_run_json(args.run_dir, Path("research-swarm-fault.json"), fault)
    print(output)
    print(
        json.dumps(
            {"status": report.get("status"), "publishedMessages": report.get("publishedMessages")}
        )
    )
    return 0 if report.get("status") == "passed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
