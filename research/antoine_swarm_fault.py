#!/usr/bin/env python3
"""Record bounded, read-only vehicle state-stream timing from NATS."""

from __future__ import annotations

import argparse
import json
import math
import selectors
import subprocess
import time
from pathlib import Path

from dvd_research.run_files import write_run_json
from dvd_research.run_nats import validate_run_nats_endpoint
from dvd_research.strict_json import loads as strict_json_loads


def matching_vehicle_state(line: str | bytes, callsign: str) -> dict | None:
    try:
        payload = strict_json_loads(line)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if isinstance(payload, dict) and payload.get("identity") == callsign:
        return payload
    return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--server", required=True)
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--fault", choices=("state-stream-timing",), required=True)
    parser.add_argument("--callsign", default="drone-2", help="observed vehicle callsign")
    parser.add_argument("--duration", type=float, default=4.0)
    parser.add_argument("--startup-timeout", type=float, default=30.0)
    parser.add_argument("--rate", type=int, default=20, help="Maximum sample rate to record")
    parser.add_argument("--max-messages", type=int, default=80)
    args = parser.parse_args()

    if (
        not 0.25 <= args.duration <= 10
        or not 0.25 <= args.startup_timeout <= 45
        or not 1 <= args.rate <= 20
        or not 1 <= args.max_messages <= 200
    ):
        raise SystemExit(
            "limits exceeded: duration 0.25..10s, startup 0.25..45s, sample rate 1..20Hz, messages 1..200"
        )
    if args.callsign not in {f"drone-{index}" for index in range(1, 6)}:
        raise SystemExit("callsign must be one of drone-1..drone-5")
    try:
        validate_run_nats_endpoint(
            args.server, args.run_dir, time.monotonic() + args.startup_timeout
        )
    except (TimeoutError, ValueError) as error:
        raise SystemExit(
            f"observer requires its run-owned loopback NATS broker: {error}"
        ) from error

    # Antoine consumes callsign-scoped state subjects, so the observer follows
    # the same identity the runner selected rather than assuming drone-2.
    subject = f"furia.gzsitl.state.{args.callsign}"
    subscription = "furia.gzsitl.state.>"
    record = {
        "contract": "damn-vulnerable-drone.antoine-swarm-fault",
        "version": "1.0.0",
        "runId": args.run_dir.name,
        "fault": args.fault,
        "callsign": args.callsign,
        "subject": subject,
        "subscription": subscription,
        "durationSeconds": args.duration,
        "startupTimeoutSeconds": args.startup_timeout,
        "maxSampleRateHz": args.rate,
        "maxMessages": args.max_messages,
        "startedAtUnixNs": time.time_ns(),
        "publishedMessages": 0,
        "mechanism": "read-only NATS state-stream receive-time observation",
        "status": "started",
        "limits": "No messages are published, delayed, dropped, or modified.",
    }

    timestamps: list[int] = []
    last_recorded_ns = 0
    startup_deadline = time.monotonic() + args.startup_timeout
    observation_deadline: float | None = None
    startup_errors: list[str] = []
    subscriber: subprocess.Popen[bytes] | None = None
    subscriber_exit_code: int | None = None

    def start_subscriber() -> subprocess.Popen[bytes]:
        return subprocess.Popen(
            ["nats", "--no-context", "--server", args.server, "sub", "--raw", subscription],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )

    try:
        with selectors.DefaultSelector() as selector:
            while len(timestamps) < args.max_messages:
                now = time.monotonic()
                deadline = observation_deadline or startup_deadline
                if now >= deadline:
                    break
                if subscriber is None:
                    try:
                        subscriber = start_subscriber()
                    except OSError as error:
                        startup_errors.append(f"subscriber could not start: {type(error).__name__}")
                        break
                    if subscriber.stdout is None:
                        raise RuntimeError("could not capture NATS subscriber output")
                    selector.register(subscriber.stdout, selectors.EVENT_READ)
                if subscriber.poll() is not None:
                    subscriber_exit_code = subscriber.returncode
                    if subscriber.stderr is not None:
                        error = (
                            subscriber.stderr.read(2048).decode("utf-8", errors="replace").strip()
                        )
                        if error:
                            startup_errors.append(error)
                    if observation_deadline is None and time.monotonic() < startup_deadline:
                        if subscriber.stdout is not None:
                            selector.unregister(subscriber.stdout)
                            subscriber.stdout.close()
                        subscriber = None
                        time.sleep(0.1)
                        continue
                    break
                ready = selector.select(min(0.1, max(0.0, deadline - now)))
                for key, _ in ready:
                    line = key.fileobj.readline()
                    if not line:
                        continue
                    if matching_vehicle_state(line, args.callsign) is None:
                        continue
                    received_ns = time.monotonic_ns()
                    if observation_deadline is None:
                        observation_deadline = time.monotonic() + args.duration
                    min_interval_ns = int(1e9 / args.rate)
                    if received_ns - last_recorded_ns >= min_interval_ns:
                        timestamps.append(received_ns)
                        last_recorded_ns = received_ns
    finally:
        if subscriber is not None:
            subscriber.terminate()
            try:
                subscriber.wait(timeout=2)
            except subprocess.TimeoutExpired:
                subscriber.kill()
                subscriber.wait()
            subscriber_exit_code = subscriber.returncode
            if subscriber.stderr is not None:
                error = subscriber.stderr.read(2048).decode("utf-8", errors="replace").strip()
                if error:
                    startup_errors.append(error)

    gaps = [
        (later - earlier) / 1e9 for earlier, later in zip(timestamps, timestamps[1:], strict=False)
    ]
    elapsed = (timestamps[-1] - timestamps[0]) / 1e9 if len(timestamps) > 1 else 0.0
    observed_rate = (len(timestamps) - 1) / elapsed if elapsed > 0 else None
    record.update(
        observedMessages=len(timestamps),
        subscriberExitCode=subscriber_exit_code,
        subscriberStderr="\n".join(startup_errors),
        maxObservedGapSeconds=round(max(gaps), 6) if gaps else None,
        observedSampleRateHz=round(observed_rate, 3) if observed_rate is not None else None,
        completedAtUnixNs=time.time_ns(),
        status="observation-only"
        if len(timestamps) >= 2 and observed_rate is not None and math.isfinite(observed_rate)
        else "inconclusive",
    )
    output = write_run_json(args.run_dir, Path("research-swarm-fault.json"), record)
    print(output)
    return 0 if record["status"] == "observation-only" else 2


if __name__ == "__main__":
    raise SystemExit(main())
