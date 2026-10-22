#!/usr/bin/env python3
"""Capture a tiny, bounded sample of the local Gazebo camera topic."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import time
from pathlib import Path

from dvd_research.run_files import write_run_json


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--gz", required=True)
    parser.add_argument("--topic", required=True)
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--startup-timeout", type=float, default=15.0)
    args = parser.parse_args()
    if not re.fullmatch(
        r"/world/antoine_gz_flight_proof/model/isr/link/base_link/sensor/camera/image", args.topic
    ):
        raise SystemExit("refusing a camera topic outside the expected local drone sensor")
    if not 1 <= args.timeout <= 30:
        raise SystemExit("camera probe timeout must be between 1 and 30 seconds")
    if not 1 <= args.startup_timeout <= 30:
        raise SystemExit("camera startup timeout must be between 1 and 30 seconds")

    env = os.environ.copy()
    partition = env.get("GZ_PARTITION")
    # Antoine pins Gazebo Transport discovery to loopback in its own launcher
    # process. This bounded same-host probe is a separate process, so preserve
    # the same local discovery address when it runs against that partition.
    env.setdefault("GZ_IP", "127.0.0.1")

    started = time.time()
    discovery_started = time.monotonic()
    discovery_deadline = discovery_started + args.startup_timeout
    topic_listed = False
    publisher_present = False
    discovery_error = None
    while True:
        try:
            topic_list = subprocess.run(
                [args.gz, "topic", "--list"],
                capture_output=True,
                text=True,
                timeout=3,
                check=False,
                env=env,
            )
            topic_listed = args.topic in set(topic_list.stdout.splitlines())
            if topic_listed:
                topic_info = subprocess.run(
                    [args.gz, "topic", "--info", "--topic", args.topic],
                    capture_output=True,
                    text=True,
                    timeout=3,
                    check=False,
                    env=env,
                )
                info_text = f"{topic_info.stdout}\n{topic_info.stderr}"
                publisher_present = (
                    "No publishers on topic" not in info_text and topic_info.returncode == 0
                )
                if publisher_present:
                    break
        except subprocess.TimeoutExpired:
            pass
        except OSError as error:
            discovery_error = type(error).__name__
            break
        if time.monotonic() >= discovery_deadline:
            break
        time.sleep(min(0.25, max(0.0, discovery_deadline - time.monotonic())))
    discovery_wait_seconds = round(time.monotonic() - discovery_started, 3)
    command = [args.gz, "topic", "--echo", "--json-output", "--num", "5", "--topic", args.topic]
    try:
        completed = subprocess.run(
            command, capture_output=True, text=True, timeout=args.timeout, check=False, env=env
        )
    except subprocess.TimeoutExpired as error:
        output = (
            error.stdout.decode(errors="replace")
            if isinstance(error.stdout, bytes)
            else (error.stdout or "")
        )
        return_code = None
        timed_out = True
        start_error = None
    except OSError as error:
        output = ""
        return_code = None
        timed_out = False
        start_error = type(error).__name__
    else:
        output = completed.stdout
        return_code = completed.returncode
        timed_out = False
        start_error = None

    message_count = 0
    valid_frame_count = 0
    malformed_image_count = 0
    dimensions: set[tuple[int, int]] = set()
    payload_bytes: set[int] = set()
    decoder = json.JSONDecoder()
    cursor = 0
    while cursor < len(output):
        start = output.find("{", cursor)
        if start < 0:
            break
        try:
            item, end = decoder.raw_decode(output, start)
        except json.JSONDecodeError:
            cursor = start + 1
            continue
        cursor = end
        if not isinstance(item, dict):
            continue
        width = item.get("width")
        height = item.get("height")
        data = item.get("data")
        valid_dimensions = type(width) is int and type(height) is int and width > 0 and height > 0
        valid_data = (isinstance(data, str) and len(data) > 0) or (
            isinstance(data, list)
            and len(data) > 0
            and all(type(value) is int and 0 <= value <= 255 for value in data)
        )
        if not valid_dimensions or not valid_data:
            malformed_image_count += 1
            continue
        message_count += 1
        valid_frame_count += 1
        dimensions.add((width, height))
        payload_bytes.add(len(data))

    report = {
        "contract": "damn-vulnerable-drone.antoine-camera-observation",
        "version": "1.0.0",
        "scenario": "gazebo-camera-readable-topic-antoine",
        "runId": args.run_dir.name,
        "topic": args.topic,
        "partition": partition,
        "topicListed": topic_listed,
        "publisherPresentAtProbe": publisher_present,
        "discoveryError": discovery_error,
        "discoveryWaitSeconds": discovery_wait_seconds,
        "captureLimit": 5,
        "capturedMessages": min(message_count, 5),
        "validImageFrames": min(valid_frame_count, 5),
        "malformedImageMessages": malformed_image_count,
        "dimensions": [{"width": width, "height": height} for width, height in sorted(dimensions)],
        "payloadSizes": sorted(payload_bytes),
        "captureDurationSeconds": round(time.time() - started, 3),
        "subscriberExitCode": return_code,
        "subscriberTimedOut": timed_out,
        "subscriberStartError": start_error,
        "imagePayloadsStored": False,
        "interpretation": (
            "The local Gazebo Transport camera topic was readable by a same-host client without credentials. "
            "Only message metadata and payload sizes were retained; this establishes local topic exposure, "
            "not a remote camera disclosure or a video-stream attack."
            if message_count
            else (
                "Gazebo listed the camera topic but reported no publisher; no image messages were captured. "
                "No camera publisher was reported during the bounded probe."
                if topic_listed and not publisher_present
                else "No image messages were captured within the bounded window; camera availability is inconclusive."
            )
        ),
    }
    args.run_dir.mkdir(parents=True, exist_ok=True)
    write_run_json(args.run_dir, Path("research-camera-observation.json"), report)
    print(
        json.dumps(
            {
                "capturedMessages": report["capturedMessages"],
                "dimensions": report["dimensions"],
                "timedOut": timed_out,
            }
        )
    )
    return 0 if valid_frame_count else 2


if __name__ == "__main__":
    raise SystemExit(main())
