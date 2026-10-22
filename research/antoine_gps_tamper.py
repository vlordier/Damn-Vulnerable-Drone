#!/usr/bin/env python3
"""Publish one altered, signed GPS envelope to a live local Antoine run."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from dvd_research.run_nats import read_run_nats_log
from dvd_research.sensor_fixture import build_case

SUBJECT = "brenta.sensor.gps.drone-1"
ACCEPTED_MARKER = "ANTOINE_GZ_GPS_FIX callsign=drone-1"


def read_log(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def count_accepted(log_path: Path) -> int:
    return read_log(log_path).count(ACCEPTED_MARKER)


def capture_messages(server: str, count: int) -> list[dict[str, Any]]:
    completed = subprocess.run(
        ["nats", "--no-context", "--server", server, "sub", f"--count={count}", "--raw", SUBJECT],
        text=True,
        capture_output=True,
        timeout=10,
        check=True,
    )
    messages = []
    for line in completed.stdout.splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            messages.append(value)
    if len(messages) != count:
        raise ValueError(f"expected {count} live GPS envelopes; captured {len(messages)}")
    return messages


def stop_forwarder(run_dir: Path, server: str) -> int:
    pid_path = run_dir / "sensor-forwarder.pid"
    pid_text = pid_path.read_text(encoding="utf-8").strip()
    if not pid_text.isdigit():
        raise ValueError("run did not record a valid sensor-forwarder PID")
    pid = int(pid_text)
    identity_path_file = run_dir / "sensor-forwarder.identity-path"
    identity_file = Path(identity_path_file.read_text(encoding="utf-8").strip())
    expected_binary = os.environ.get("FURIA_GZ_SENSOR_FORWARDER_BIN", "")
    command = subprocess.run(
        ["ps", "-ww", "-p", str(pid), "-o", "command="], text=True, capture_output=True, check=False
    ).stdout.strip()
    if not expected_binary or expected_binary not in command or "--nats-url" not in command:
        raise ValueError(
            "refusing to signal a PID that does not match this run's configured forwarder"
        )
    command_tokens = command.split()
    try:
        configured_server = command_tokens[command_tokens.index("--nats-url") + 1]
    except (ValueError, IndexError):
        raise ValueError("run forwarder command has no verifiable NATS endpoint") from None
    if configured_server != server:
        raise ValueError("refusing to stop a sensor forwarder attached to another broker")
    supervisor_identity = dict(
        line.split("=", 1)
        for line in identity_file.read_text(encoding="utf-8").splitlines()
        if "=" in line
    )
    if str(supervisor_identity.get("child_pid")) != str(pid):
        raise ValueError("recorded supervisor does not own the run's sensor-forwarder PID")
    supervisor = os.environ.get("DVD_PROCESS_SUPERVISOR", "")
    if not supervisor or not Path(supervisor).is_file():
        raise ValueError("native process supervisor is unavailable")
    stopped = subprocess.run([supervisor, "stop", str(identity_file)], check=False)
    if stopped.returncode != 0:
        raise ValueError("native supervisor refused to stop the recorded sensor forwarder")
    for _ in range(80):
        check = subprocess.run(
            ["ps", "-ww", "-p", str(pid), "-o", "command="],
            text=True,
            capture_output=True,
            check=False,
        ).stdout.strip()
        if not check:
            return pid
        state = subprocess.run(
            ["ps", "-o", "stat=", "-p", str(pid)],
            text=True,
            capture_output=True,
            check=False,
        ).stdout.strip()
        if state.startswith("Z"):
            return pid
        time.sleep(0.05)
    raise TimeoutError("the run-owned sensor forwarder did not stop")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--server", required=True)
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument(
        "--case",
        choices=[
            "signature-mutation",
            "duplicate-sequence",
            "out-of-order-sequence",
            "stale-sample",
            "future-sample",
        ],
        default="signature-mutation",
    )
    args = parser.parse_args()
    parsed = urlsplit(args.server)
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
        raise SystemExit("refusing any endpoint except an explicit IPv4 loopback NATS URL")

    run_dir = args.run_dir
    log_path = run_dir / "logs" / "antoine-controller.log"
    forwarder_log = run_dir / "logs" / "gz-nav-sensor-forwarder.log"
    report: dict[str, Any] = {
        "contract": "damn-vulnerable-drone.antoine-gps-tampering-result",
        "version": "1.0.0",
        "scenario": (
            "gps-signature-tampering-antoine"
            if args.case == "signature-mutation"
            else f"gps-{args.case}-antoine"
        ),
        "case": args.case,
        "scope": "one altered signed GPS message on a run-owned loopback broker; test-only sensor key",
        "subject": SUBJECT,
        "publishedMessages": 1,
        "clientAuthentication": "none",
        "modifiedField": "lat_deg"
        if args.case == "signature-mutation"
        else (
            "sequence"
            if args.case in {"duplicate-sequence", "out-of-order-sequence"}
            else "timestamp_ms"
        ),
        "runId": run_dir.name,
    }
    try:
        deadline = time.monotonic() + 100
        ready_line = f"Listening for client connections on 127.0.0.1:{parsed.port}"
        while time.monotonic() < deadline:
            broker_log = read_run_nats_log(run_dir) or ""
            if ready_line in broker_log and "Server is ready" in broker_log:
                break
            time.sleep(0.1)
        else:
            raise TimeoutError(
                "run-owned NATS broker did not confirm the requested loopback endpoint"
            )
        while time.monotonic() < deadline:
            controller_ready = ACCEPTED_MARKER in read_log(log_path)
            forwarder_ready = "streams ready; sensor-only forwarding enabled" in read_log(
                forwarder_log
            )
            identity_path_marker = run_dir / "sensor-forwarder.identity-path"
            if (
                controller_ready
                and forwarder_ready
                and (run_dir / "sensor-forwarder.pid").is_file()
                and identity_path_marker.is_file()
            ):
                pid = int((run_dir / "sensor-forwarder.pid").read_text(encoding="utf-8").strip())
                identity_path = Path(identity_path_marker.read_text(encoding="utf-8").strip())
                if not identity_path.is_file():
                    time.sleep(0.1)
                    continue
                metadata = dict(
                    line.split("=", 1)
                    for line in identity_path.read_text(encoding="utf-8").splitlines()
                    if "=" in line
                )
                if metadata.get("child_pid") != str(pid):
                    raise ValueError("sensor forwarder PID is not owned by its recorded supervisor")
                break
            time.sleep(0.1)
        else:
            raise TimeoutError(
                "signed sensor forwarder and Antoine GPS verifier did not both become ready"
            )

        captured_messages = capture_messages(
            args.server, 2 if args.case == "out-of-order-sequence" else 1
        )
        payload = captured_messages[0]
        auth = payload.get("authentication")
        if not isinstance(auth, dict) or auth.get("contract") != "furia.sensor.signature.v1":
            raise ValueError("captured GPS sample has no expected Ed25519 authentication envelope")
        if payload.get("schema") != "brenta.sensor.gps.v1" or payload.get("identity") != "drone-1":
            raise ValueError("captured GPS sample identity or schema did not match this run")
        if payload.get("frame_id") != "isr::base_link::gps":
            raise ValueError(
                "captured GPS source frame did not match the configured callsign mapping"
            )
        timestamp_ms = payload.get("timestamp_ms")
        expires_at_ms = payload.get("expires_at_ms")
        if not isinstance(timestamp_ms, int) or not isinstance(expires_at_ms, int):
            raise ValueError("captured GPS sample has no bounded freshness timestamps")

        stopped_pid = stop_forwarder(run_dir, args.server)
        baseline_count = count_accepted(log_path)
        now_ms = int(time.time() * 1000)
        original_latitude = payload.get("lat_deg")
        injected = payload
        latest_sequence = payload.get("sequence")
        if args.case == "signature-mutation":
            age_ms = now_ms - timestamp_ms
            if age_ms < -250 or age_ms > 250 or now_ms >= expires_at_ms:
                raise TimeoutError(
                    "captured signed GPS sample expired before the bounded tampering probe"
                )
            if not isinstance(original_latitude, (int, float)):
                raise ValueError("captured GPS sample has no numeric latitude")
            injected = dict(payload)
            injected["lat_deg"] = original_latitude + 0.01
        elif args.case == "out-of-order-sequence":
            latest_sequence = captured_messages[-1].get("sequence")
            if (
                type(latest_sequence) is not int
                or type(payload.get("sequence")) is not int
                or latest_sequence <= payload["sequence"]
            ):
                raise ValueError("captured messages do not establish an increasing sequence order")
            injected = build_case(payload, "out-of-order-sequence", now_ms)
            if injected["sequence"] >= latest_sequence:
                raise ValueError(
                    "constructed sequence is not older than the latest captured sample"
                )
        else:
            fixture_case = {
                "duplicate-sequence": "duplicate-sequence",
                "stale-sample": "stale-sample",
                "future-sample": "future-sample",
            }[args.case]
            injected = build_case(payload, fixture_case, now_ms)

        published_at_ms = int(time.time() * 1000)
        subprocess.run(
            [
                "nats",
                "--no-context",
                "--server",
                args.server,
                "pub",
                SUBJECT,
                json.dumps(injected, separators=(",", ":")),
            ],
            text=True,
            capture_output=True,
            timeout=8,
            check=True,
        )
        observation_deadline = time.monotonic() + 1.0
        while time.monotonic() < observation_deadline:
            time.sleep(0.05)
        final_count = count_accepted(log_path)
        accepted_delta = final_count - baseline_count
        case_observation = {
            "signature-mutation": "altered-signature-rejected"
            if accepted_delta == 0
            else "altered-signature-ingress-accepted",
            "duplicate-sequence": "fresh-valid-signature-with-duplicate-sequence-accepted"
            if accepted_delta == 1
            else "duplicate-sequence-ingress-not-observed",
            "out-of-order-sequence": "fresh-valid-signature-with-older-sequence-accepted"
            if accepted_delta == 1
            else "older-sequence-ingress-not-observed",
            "stale-sample": "stale-valid-signature-passed-ingress"
            if accepted_delta == 1
            else "stale-sample-ingress-not-observed",
            "future-sample": "future-valid-signature-passed-ingress"
            if accepted_delta == 1
            else "future-sample-ingress-not-observed",
        }[args.case]
        report.update(
            {
                "capturedSignedMessage": True,
                "authenticationContract": injected.get("authentication", {}).get(
                    "contract", auth.get("contract")
                ),
                "keyId": injected.get("authentication", {}).get("key_id", auth.get("key_id")),
                "signatureValueStored": False,
                "fixtureSignatureGenerated": args.case
                in {"duplicate-sequence", "out-of-order-sequence", "stale-sample", "future-sample"},
                "frameId": injected.get("frame_id"),
                "capturedSequence": payload.get("sequence"),
                "latestCapturedSequence": latest_sequence,
                "injectedSequence": injected.get("sequence"),
                "injectedSequenceOlderThanLatestCaptured": (
                    type(latest_sequence) is int
                    and type(injected.get("sequence")) is int
                    and injected["sequence"] < latest_sequence
                )
                if args.case == "out-of-order-sequence"
                else None,
                "sampleAgeAtPublishMs": published_at_ms
                - injected.get("timestamp_ms", timestamp_ms),
                "sampleExpiresAtUnixMs": injected.get("expires_at_ms", expires_at_ms),
                "latDeltaDegrees": round(float(injected["lat_deg"] - original_latitude), 6)
                if isinstance(original_latitude, (int, float))
                else 0.0,
                "signatureRetainedAfterMutation": args.case == "signature-mutation",
                "forwarderPidStopped": stopped_pid,
                "acceptedGpsTraceCountBefore": baseline_count,
                "acceptedGpsTraceCountAfter": final_count,
                "acceptedGpsTraceDelta": accepted_delta,
                "tamperingRejectedObserved": accepted_delta == 0
                if args.case == "signature-mutation"
                else None,
                "sensorIngressAccepted": accepted_delta == 1,
                "freshnessUseDecisionObserved": False,
                "validFixtureSignature": args.case != "signature-mutation",
                "freshnessInterpretation": (
                    "Controller ingress trace records signature/schema/frame acceptance only; the downstream freshness-use decision has no exposed trace in this build."
                    if args.case in {"stale-sample", "future-sample"}
                    else None
                ),
                "publishedAtUnixMs": published_at_ms,
                "caseObservation": case_observation,
                "result": case_observation,
                "interpretation": (
                    "A live signed GPS message was captured, its latitude changed while retaining the original signature, and the run-owned forwarder was stopped before one replay. No additional controller GPS acceptance trace followed. This is bounded negative evidence for this test-only local profile; it does not establish physical GPS integrity or general spoofing resistance."
                    if args.case == "signature-mutation" and accepted_delta == 0
                    else "A run-local Ed25519 fixture signed one controlled envelope. The controller emitted one sensor-ingress acceptance trace. This demonstrates ingress acceptance only; it does not show whether the navigation freshness gate used the sample or establish a movement effect."
                    if accepted_delta == 1
                    else "No sensor-ingress acceptance trace followed the single run-local signed fixture. The current controller does not expose a rejection reason, so this does not distinguish freshness rejection from another downstream contract check."
                ),
            }
        )
        output = run_dir / "research-gps-tampering.json"
        output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(json.dumps(report, indent=2))
        expected_delta = 0 if args.case in {"signature-mutation", "future-sample"} else 1
        return 0 if accepted_delta == expected_delta else 2
    except Exception as error:
        report.update(
            {
                "result": "inconclusive",
                "tamperingRejectedObserved": None,
                "error": f"{type(error).__name__}: {error}",
            }
        )
        output = run_dir / "research-gps-tampering.json"
        output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(json.dumps(report, indent=2), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
