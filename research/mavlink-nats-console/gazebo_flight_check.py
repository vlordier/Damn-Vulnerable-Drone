#!/usr/bin/env python3
"""Exercise the live MAVLink/NATS bridge against independent Gazebo poses."""

from __future__ import annotations

import asyncio
import json
import math
import os
import re
import sys
import time
from pathlib import Path

from flight_check import FlightCheck
from flight_check_support import finite_json_number, require_check

from dvd_research.run_files import read_run_tail_text, resolve_run_file, write_run_json

POSE_SAMPLE_MAX_AGE_SECONDS = 1.5
MAVLINK_STATE_MAX_AGE_SECONDS = 1.5
TELEMETRY_MAX_AGE_MS = 1_000
TELEMETRY_MAX_FUTURE_MS = 250
ACTUATOR_AUDIT_MAX_BYTES = 2_000_000
ACTUATOR_OUTPUT_CONTRACT = "furia.gzsitl.actuator-output-audit"
ACTUATOR_OUTPUT_VERSION = "1.0.0"
AUTHORIZED_ACTUATOR_STAGES = {"authorized-command", "startup-neutral"}
SHA256_WORLD_DIGEST = re.compile(r"sha256:[0-9a-fA-F]{64}")


def gazebo_timestamp_seconds(value: dict) -> float | None:
    header = value.get("header")
    stamp = header.get("stamp") if isinstance(header, dict) else None
    if not isinstance(stamp, dict):
        return None
    sec = stamp.get("sec")
    nsec = stamp.get("nsec", 0)
    if isinstance(sec, str) and len(sec) <= 19 and sec.isascii() and sec.isdecimal():
        sec = int(sec)
    if isinstance(nsec, str) and len(nsec) <= 9 and nsec.isascii() and nsec.isdecimal():
        nsec = int(nsec)
    if (
        not isinstance(sec, int)
        or isinstance(sec, bool)
        or not isinstance(nsec, int)
        or isinstance(nsec, bool)
        or sec < 0
        or sec > (1 << 63) - 1
        or not 0 <= nsec < 1_000_000_000
    ):
        return None
    return sec + nsec / 1_000_000_000


def write_failed_report(
    run_directory: Path,
    callsign: str,
    failure: Exception,
    cleanup: dict,
    check: GazeboFlightCheck,
) -> Path:
    return write_run_json(
        run_directory,
        Path("gazebo-mavlink-acceptance.json"),
        {
            "schema": "dvd.gazebo-mavlink-flight-check.v1",
            "passed": False,
            "callsign": callsign,
            "phase": getattr(check, "phase", None),
            "failure": {
                "type": type(failure).__name__,
                "message": str(failure)[:2000],
            },
            "cleanup": cleanup,
            "initialHomePose": getattr(check, "home_pose", None),
            "lastGazeboPose": check.pose(),
            "lastGazeboPoseObservation": check.pose_observation(),
            "lastMavlinkState": dict(check.telemetry),
            "rawGazeboTelemetry": check.raw_telemetry,
            "validGazeboTelemetryReceived": getattr(
                check, "valid_telemetry_received", asyncio.Event()
            ).is_set(),
            "telemetryRejection": getattr(check, "telemetry_rejection", None),
            "auditFrameCount": len(check.audit_frames),
        },
    )


class GazeboFlightCheck(FlightCheck):
    def __init__(
        self,
        url: str,
        bind_port: int,
        bridge_port: int,
        callsign: str,
        pose_file: Path,
        run_directory: Path,
        agent: str,
    ):
        super().__init__(url, bind_port, bridge_port, callsign)
        self.pose_file = pose_file
        self.run_directory = run_directory
        expected_pose = run_directory / "flight-proof" / "gazebo-pose.jsonl"
        if pose_file.absolute() != expected_pose.absolute():
            raise ValueError("Gazebo pose file must be the selected run's flight-proof artifact")
        self.run_id = os.environ.get("ANTOINE_GZ_FLIGHT_RUN_ID", run_directory.name)
        self.agent = agent
        self.raw_telemetry: dict = {}
        self.telemetry_rejection: str | None = None
        self.valid_telemetry_received = asyncio.Event()
        self.last_observed_at_ms: int | None = None
        self.phase = "connecting"
        self.home_pose: tuple[float, float, float] | None = None
        self.keepalive_task: asyncio.Task | None = None
        self.vehicle_state_updated_at: float | None = None
        self.state_field_updated_at: dict[str, float] = {}
        self.mode_names = {0: "STABILIZE", 1: "GUIDED", 2: "AUTO", 3: "HOLD", 4: "RTL", 5: "LAND"}

    async def on_telemetry(self, msg):
        try:
            value = json.loads(msg.data)
        except (UnicodeDecodeError, json.JSONDecodeError):
            self.telemetry_rejection = "telemetry is not valid JSON"
            return
        if not isinstance(value, dict):
            self.telemetry_rejection = "telemetry is not a JSON object"
            return
        if (
            value.get("schema") != "furia.telemetry"
            or value.get("version") != "1.0.0"
            or value.get("identity") != self.callsign
            or value.get("protocol") != "antoine-gz"
            or value.get("runId") != self.run_id
        ):
            self.telemetry_rejection = (
                "telemetry schema, Gazebo protocol, callsign, or run ID does not match this profile"
            )
            return
        observed_at_ms = value.get("observedAtUnixMs")
        now_ms = time.time_ns() // 1_000_000
        if (
            not isinstance(observed_at_ms, int)
            or isinstance(observed_at_ms, bool)
            or observed_at_ms < now_ms - TELEMETRY_MAX_AGE_MS
            or observed_at_ms > now_ms + TELEMETRY_MAX_FUTURE_MS
        ):
            self.telemetry_rejection = "Gazebo telemetry observation timestamp is stale or invalid"
            return
        if self.last_observed_at_ms is not None and observed_at_ms <= self.last_observed_at_ms:
            self.telemetry_rejection = "Gazebo telemetry observation timestamp did not advance"
            return
        self.raw_telemetry = value
        self.last_observed_at_ms = observed_at_ms
        self.telemetry_rejection = None
        self.valid_telemetry_received.set()

    async def start(self):
        await super().start()
        self.keepalive_task = asyncio.create_task(self.keepalive())

    async def keepalive(self):
        while True:
            self.send_heartbeat()
            await asyncio.sleep(0.5)

    def _update_vehicle_state(self, message) -> None:
        message_type = message.get_type()
        state = dict(self.telemetry)
        updated_at = time.monotonic()
        if message_type == "HEARTBEAT":
            state["armed"] = bool(message.base_mode & 128)
            state["mode"] = self.mode_names.get(
                message.custom_mode, f"UNKNOWN_{message.custom_mode}"
            )
            self.state_field_updated_at["armed"] = updated_at
            self.state_field_updated_at["mode"] = updated_at
        elif message_type == "GLOBAL_POSITION_INT":
            state["alt_m"] = message.relative_alt / 1000.0
            state["lat_deg"] = message.lat / 1e7
            state["lon_deg"] = message.lon / 1e7
            state["vn_mps"] = message.vx / 100.0
            state["ve_mps"] = message.vy / 100.0
            state["vd_mps"] = message.vz / 100.0
            for field in ("alt_m", "lat_deg", "lon_deg", "vn_mps", "ve_mps", "vd_mps"):
                self.state_field_updated_at[field] = updated_at
        elif message_type == "SYS_STATUS":
            state["battery_pct"] = message.battery_remaining
            state["voltage_battery_mv"] = message.voltage_battery
            state["prearm_ready"] = bool(message.onboard_control_sensors_health)
            self.state_field_updated_at["prearm_ready"] = updated_at
        self.telemetry = state
        if message_type in {"HEARTBEAT", "GLOBAL_POSITION_INT", "SYS_STATUS"}:
            self.vehicle_state_updated_at = updated_at

    async def recv_messages(self, seconds=0.5):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            try:
                raw, _ = self.socket.recvfrom(4096)
                self.received_frames.append(raw.hex())
                self.received_frames = self.received_frames[-12:]
                try:
                    decoded = self.parser.parse_buffer(raw)
                except Exception:
                    continue
                for message in decoded or []:
                    self.frames.append(raw)
                    self._update_vehicle_state(message)
                    yield None, message
            except TimeoutError:
                await asyncio.sleep(0)

    async def wait_state(
        self,
        predicate,
        timeout_s: float,
        description: str,
        *,
        fresh_fields: tuple[str, ...] = (),
    ):
        started_at = time.monotonic()
        deadline = started_at + timeout_s

        def fresh_state_observed() -> bool:
            if fresh_fields:
                return all(
                    self.state_field_updated_at.get(field, 0.0) > started_at
                    for field in fresh_fields
                )
            return (
                self.vehicle_state_updated_at is not None
                and self.vehicle_state_updated_at > started_at
            )

        while time.monotonic() < deadline:
            if fresh_state_observed() and predicate(self.telemetry):
                return dict(self.telemetry)
            async for _, _message in self.recv_messages(0.1):
                if fresh_state_observed() and predicate(self.telemetry):
                    return dict(self.telemetry)
        raise AssertionError(f"timed out waiting for {description}: {self.telemetry}")

    def pose_observation(self) -> dict | None:
        try:
            pose_path = resolve_run_file(self.run_directory, Path("flight-proof/gazebo-pose.jsonl"))
            if pose_path is None:
                return None
            file_stat = pose_path.stat()
            age = time.time() - file_stat.st_mtime
            if age < -0.25 or age > POSE_SAMPLE_MAX_AGE_SECONDS:
                return None
            content = read_run_tail_text(
                self.run_directory,
                Path("flight-proof/gazebo-pose.jsonl"),
                512_000,
                errors="replace",
            )
            if content is None:
                return None
            lines = content.splitlines()
        except (FileNotFoundError, OSError, ValueError):
            return None
        for line in reversed(lines):
            try:
                value = json.loads(line)
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            if not isinstance(value, dict):
                continue
            simulation_time = gazebo_timestamp_seconds(value)
            poses = value.get("pose")
            if simulation_time is None or not isinstance(poses, list):
                continue
            for item in poses:
                if not isinstance(item, dict):
                    continue
                if item.get("name") == self.agent:
                    position = item.get("position", {})
                    if not isinstance(position, dict):
                        continue
                    coordinates = [position.get(axis) for axis in ("x", "y", "z")]
                    if not all(finite_json_number(axis) for axis in coordinates):
                        continue
                    position = tuple(float(axis) for axis in coordinates)
                    return {
                        "simulationTimeSeconds": simulation_time,
                        "position": position,
                        "fileAgeSeconds": max(0.0, age),
                    }
        return None

    def pose(self) -> tuple[float, float, float] | None:
        observation = self.pose_observation()
        return observation["position"] if observation is not None else None

    async def wait_pose(self, predicate, timeout_s: float, description: str):
        deadline = time.monotonic() + timeout_s
        last = None
        previous_simulation_time = None
        while time.monotonic() < deadline:
            observation = self.pose_observation()
            if observation is not None:
                simulation_time = observation["simulationTimeSeconds"]
                last = observation["position"]
                if (
                    previous_simulation_time is not None
                    and simulation_time < previous_simulation_time
                ):
                    raise AssertionError(
                        f"Gazebo simulation time moved backwards while waiting for {description}: "
                        f"{previous_simulation_time} -> {simulation_time}"
                    )
                if (
                    previous_simulation_time is not None
                    and simulation_time > previous_simulation_time
                ):
                    if predicate(last):
                        return last
                previous_simulation_time = simulation_time
            await asyncio.sleep(0.1)
        raise AssertionError(
            f"timed out waiting for an advancing Gazebo pose {description}: {last}"
        )

    async def wait_settled_home(self, home: tuple[float, float, float], timeout_s=8.0):
        """Require fresh MAVLink state and Gazebo truth to stay settled before disarm."""
        deadline = time.monotonic() + timeout_s
        settled_since = None
        last_state = {}
        last_pose = None
        last_pose_simulation_time = None
        while time.monotonic() < deadline:
            async for _, _message in self.recv_messages(0.1):
                pass
            last_state = dict(self.telemetry)
            pose_observation = self.pose_observation()
            last_pose = pose_observation["position"] if pose_observation is not None else None
            if (
                pose_observation is not None
                and last_pose_simulation_time is not None
                and pose_observation["simulationTimeSeconds"] < last_pose_simulation_time
            ):
                raise AssertionError(
                    "Gazebo simulation time moved backwards during settled-home verification"
                )
            current_time = time.monotonic()
            state_is_fresh = (
                self.vehicle_state_updated_at is not None
                and 0.0
                <= current_time - self.vehicle_state_updated_at
                <= MAVLINK_STATE_MAX_AGE_SECONDS
            )
            pose_is_advancing = pose_observation is not None and (
                last_pose_simulation_time is None
                or pose_observation["simulationTimeSeconds"] > last_pose_simulation_time
            )
            if pose_observation is not None:
                last_pose_simulation_time = pose_observation["simulationTimeSeconds"]
            settled = (
                state_is_fresh
                and pose_is_advancing
                and last_state.get("armed") is True
                and last_state.get("alt_m", 999.0) <= 0.20
                and abs(last_state.get("vn_mps", 999.0)) <= 0.15
                and abs(last_state.get("ve_mps", 999.0)) <= 0.15
                and abs(last_state.get("vd_mps", 999.0)) <= 0.15
                and last_pose is not None
                and math.dist(last_pose, home) <= 0.25
            )
            if settled:
                if settled_since is None:
                    settled_since = time.monotonic()
                elif time.monotonic() - settled_since >= 0.75:
                    return last_state, last_pose
            else:
                settled_since = None
            await asyncio.sleep(0.05)
        raise AssertionError(
            f"Gazebo did not remain settled at home before disarm: "
            f"MAVLink={last_state}, Gazebo={last_pose}"
        )

    async def disarm_after_settle(self, home: tuple[float, float, float]):
        last_error = None
        for _attempt in range(5):
            await self.wait_settled_home(home)
            try:
                return await self.request(self.long(400, p1=0.0), 400)
            except AssertionError as error:
                last_error = error
                await asyncio.sleep(0.2)
        raise AssertionError(f"disarm remained denied after settled retries: {last_error}")

    async def set_mode(self, custom_mode: int) -> None:
        await self.request(
            self.long(176, p1=1.0, p2=float(custom_mode)),
            176,
        )
        await self.wait_state(
            lambda state: state.get("mode") == self.mode_names[custom_mode],
            4,
            f"{self.mode_names[custom_mode]} heartbeat",
            fresh_fields=("mode",),
        )

    async def direct_control(
        self,
        command: dict,
        *,
        request_id: str,
        identity: str | None = None,
        extra_fields: dict | None = None,
    ) -> dict:
        """Publish directly to the local authority, bypassing MAVLink ingress."""
        envelope = {
            "schema": "furia.mavlink.control",
            "version": "1.0.0",
            "identity": identity or self.callsign,
            "request_id": request_id,
            "command": command,
        }
        if extra_fields:
            envelope.update(extra_fields)
        reply = await self.nc.request(
            f"furia.mavlink.control.{self.callsign}",
            json.dumps(envelope).encode(),
            timeout=2,
        )
        response = json.loads(reply.data)
        response["requestIdMatched"] = response.get("requestId") == request_id
        require_check(response["requestIdMatched"], response)
        return response

    def actuator_audit(self) -> list[dict]:
        """Read independent Gazebo applier output, not Antoine telemetry."""
        try:
            content = read_run_tail_text(
                self.run_directory,
                Path("flight-proof/applier-neutral/actuator-output.jsonl"),
                ACTUATOR_AUDIT_MAX_BYTES,
                errors="replace",
            )
        except (OSError, ValueError):
            return []
        if content is None:
            return []
        records = []
        for line in content.splitlines():
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if (
                isinstance(record, dict)
                and record.get("contract") == ACTUATOR_OUTPUT_CONTRACT
                and record.get("version") == ACTUATOR_OUTPUT_VERSION
                and record.get("runId") == self.run_id
                and record.get("vehicleId") == self.callsign
                and record.get("actuatorKind") == "motors"
            ):
                records.append(record)
        return records

    async def wait_authorized_motor_output(
        self,
        after_sequence: int,
        expected_target_m: tuple[float, float, float],
        timeout_s: float = 5.0,
    ) -> dict:
        deadline = time.monotonic() + timeout_s
        expected_target_mm = [round(value * 1000) for value in expected_target_m]
        while time.monotonic() < deadline:
            for record in self.actuator_audit():
                grant = record.get("executionGrant")
                target = grant.get("targetEnuMm") if isinstance(grant, dict) else None
                values = record.get("values")
                velocities = values.get("velocity") if isinstance(values, dict) else None
                if (
                    record.get("runId") == self.run_id
                    and type(record.get("sequence")) is int
                    and record["sequence"] > after_sequence
                    and record.get("neutral") is False
                    and record.get("authority") == "verified-core-grant"
                    and record.get("stage") in AUTHORIZED_ACTUATOR_STAGES
                    and isinstance(grant, dict)
                    and grant.get("contract") == "furia.prime.execution-grant"
                    and grant.get("vehicleId") == self.callsign
                    and grant.get("runId") == self.run_id
                    and isinstance(record.get("worldSha256"), str)
                    and SHA256_WORLD_DIGEST.fullmatch(record["worldSha256"])
                    and isinstance(record.get("frameId"), str)
                    and bool(record["frameId"])
                    and grant.get("worldDigest") == record["worldSha256"]
                    and grant.get("worldFrameId") == record["frameId"]
                    and isinstance(velocities, list)
                    and len(velocities) == 4
                    and all(finite_json_number(value) for value in velocities)
                    and any(value != 0 for value in velocities)
                    and isinstance(target, list)
                    and len(target) == 3
                    and all(
                        type(value) is int and abs(value - expected) <= 250
                        for value, expected in zip(target, expected_target_mm, strict=True)
                    )
                ):
                    return record
            await asyncio.sleep(0.1)
        raise AssertionError(
            f"no independent authorized motor output matched RTL home cruise target "
            f"{expected_target_mm}; "
            f"last records={self.actuator_audit()[-3:]}"
        )

    async def close(self):
        if self.keepalive_task:
            self.keepalive_task.cancel()
            await asyncio.gather(self.keepalive_task, return_exceptions=True)
        await super().close()


async def run_check() -> dict:
    url = os.environ.get("MAVLINK_NATS_URL", "nats://127.0.0.1:14226")
    callsign = os.environ.get("MAVLINK_CALLSIGN", "drone-1")
    agent = os.environ.get("ANTOINE_GZ_AGENT", "isr")
    pose_file = Path(os.environ["ANTOINE_GZ_POSE_FILE"])
    run_directory = Path(os.environ["ANTOINE_GZ_RUN_DIR"])
    bind_port = int(os.environ.get("MAVLINK_GAZEBO_CHECK_PORT", "14574"))
    bridge_port = int(os.environ.get("QGC_BRIDGE_PORT", "14651"))
    check = GazeboFlightCheck(
        url, bind_port, bridge_port, callsign, pose_file, run_directory, agent
    )
    home = None
    armed = False
    try:
        await check.start()
        check.phase = "waiting-for-valid-gazebo-telemetry"
        try:
            await asyncio.wait_for(check.valid_telemetry_received.wait(), timeout=3.0)
        except TimeoutError as error:
            raise AssertionError(
                "did not receive valid Antoine Gazebo-profile NATS telemetry"
                + (f": {check.telemetry_rejection}" if check.telemetry_rejection else "")
            ) from error
        check.phase = "waiting-for-prearm"
        await check.wait_state(
            lambda state: state.get("prearm_ready") is True,
            5,
            "Gazebo model pre-arm-ready SYS_STATUS",
            fresh_fields=("prearm_ready",),
        )
        initial_state = dict(check.telemetry)
        initial = await check.wait_pose(lambda point: True, 3, "initial pose")
        home = initial
        check.home_pose = home
        check.phase = "mission-clear"
        if not check.telemetry.get("prearm_ready"):
            raise AssertionError(f"Gazebo model is not pre-arm ready: {check.telemetry}")
        # Always exercise and audit MISSION_CLEAR_ALL, including on a fresh
        # profile. Conditional clearing made the final audit gate depend on
        # whether a previous run happened to leave a mission behind.
        await check.mission_clear()
        mission_count, mission = await check.mission_list()
        require_check((mission_count, mission) == (0, []), (mission_count, mission))

        check.phase = "arm"
        # The command may take effect even if its ACK is lost. From this point
        # onward, failure handling must assume the vehicle could be armed.
        armed = True
        await check.request(check.long(400, p1=1.0), 400)
        await check.wait_state(
            lambda state: state.get("armed") is True,
            4,
            "armed heartbeat",
            fresh_fields=("armed",),
        )

        await check.request(check.long(22, p7=1.0), 22)
        check.phase = "takeoff"
        await check.wait_state(
            lambda state: state.get("mode") == "GUIDED",
            4,
            "GUIDED mode",
            fresh_fields=("mode",),
        )
        takeoff = await check.wait_pose(
            lambda point: point[2] >= home[2] + 0.75, 30, "takeoff above 0.75 m"
        )

        await check.set_mode(3)
        check.phase = "position-flight"
        await check.set_mode(1)
        # Antoine's local controller captures horizontal targets inside 0.75 m.
        # Keep this waypoint beyond that band so the check proves actual travel,
        # rather than asking for a target that is mostly inside the arrival hold.
        position_target_north = 2.0
        check.socket.sendto(check.position(position_target_north, 0.0, -1.0), check.bridge)
        moved = await check.wait_pose(
            lambda point: point[0] >= home[0] + 1.0, 35, "north position target"
        )

        waypoints = [[2.0, 0.0, -1.0], [2.0, 0.0, -0.5], [0.0, 0.0, -0.5]]
        await check.mission_upload(waypoints)
        check.phase = "mission-flight"
        mission_count, mission_readback = await check.mission_list()
        require_check(mission_count == len(waypoints), mission_count)
        require_check(
            all(
                math.isclose(actual, expected, abs_tol=1e-4)
                for actual_point, expected_point in zip(mission_readback, waypoints, strict=True)
                for actual, expected in zip(actual_point, expected_point, strict=True)
            ),
            mission_readback,
        )
        mission_response = await check.nc.request(
            f"furia.mavlink.control.{callsign}",
            json.dumps(
                {
                    "schema": "furia.mavlink.control",
                    "version": "1.0.0",
                    "identity": callsign,
                    "request_id": "gazebo-mission-query",
                    "command": {"type": "mission_query"},
                }
            ).encode(),
            timeout=2,
        )
        mission_state = json.loads(mission_response.data)
        require_check(mission_state.get("missionUploaded") is True, mission_state)
        require_check(len(mission_state.get("waypoints", [])) == len(waypoints), mission_state)
        await check.request(check.long(300), 300)
        await check.wait_state(
            lambda state: state.get("mode") == "AUTO",
            4,
            "AUTO mode",
            fresh_fields=("mode",),
        )
        mission_end = await check.wait_pose(
            lambda point: math.dist(point, (home[0] + 2.0, home[1], home[2] + 0.5)) <= 1.0,
            40,
            "AUTO mission terminal waypoint",
        )

        attack_start = await check.wait_pose(
            lambda point: math.dist(point, (home[0] + 2.0, home[1], home[2] + 0.5)) <= 0.65,
            35,
            "AUTO mission target before direct-NATS boundary probes",
        )
        check.phase = "direct-nats-boundary-probes"
        wrong_callsign = await check.direct_control(
            {"type": "rtl"},
            request_id="gazebo-rtl-wrong-callsign",
            identity=f"{callsign}-spoofed",
        )
        require_check(wrong_callsign.get("accepted") is False, wrong_callsign)
        unsupported_return_target = await check.direct_control(
            {"type": "set_return_target", "payload": {"north_m": 2.0, "east_m": 2.0}},
            request_id="gazebo-unsupported-return-target",
        )
        require_check(unsupported_return_target.get("accepted") is False, unsupported_return_target)
        require_check(
            "unsupported command type" in unsupported_return_target.get("reason", ""),
            unsupported_return_target,
        )
        out_of_envelope = await check.direct_control(
            {
                "type": "set_position",
                "payload": {"north_m": 4.0, "east_m": 0.0, "down_m": -1.0},
            },
            request_id="gazebo-position-out-of-envelope",
        )
        require_check(out_of_envelope.get("accepted") is False, out_of_envelope)
        require_check(
            out_of_envelope.get("reason") == "position-out-of-lab-envelope", out_of_envelope
        )

        actuator_before_rtl = check.actuator_audit()
        actuator_sequence_before_rtl = max(
            (row.get("sequence", -1) for row in actuator_before_rtl), default=-1
        )
        caller_return_target = [home[0] + 5.0, home[1] + 5.0, home[2] + 2.0]
        rtl_command = {
            "type": "rtl",
            "payload": {
                "return_target_enu_m": caller_return_target,
                "north_m": 2.0,
                "east_m": 2.0,
                "down_m": -2.0,
            },
        }
        unsigned_rtl = await check.direct_control({"type": "rtl"}, request_id="gazebo-rtl-unsigned")
        stale_run_rtl = await check.direct_control(
            {"type": "rtl"},
            request_id="gazebo-rtl-stale-run",
            extra_fields={"runId": "stale-run-without-authority"},
        )
        stale_task_rtl = await check.direct_control(
            {"type": "rtl"},
            request_id="gazebo-rtl-unrelated-task",
            extra_fields={"taskId": "unrelated-task"},
        )
        stale_grant_rtl = await check.direct_control(
            {"type": "rtl"},
            request_id="gazebo-rtl-fake-expired-grant",
            extra_fields={
                "executionGrant": {
                    "signature": "expired-untrusted-grant",
                    "expiresAtUnixMs": 0,
                }
            },
        )
        caller_target_rtl = await check.direct_control(
            rtl_command,
            request_id="gazebo-rtl-caller-target-override",
        )
        rtl_probes = {
            "unsigned": {
                "signaturePresent": False,
                "runIdPresent": False,
                "taskIdPresent": False,
                "executionGrantPresent": False,
                "response": unsigned_rtl,
            },
            "staleRun": {
                "signaturePresent": False,
                "runIdPresent": True,
                "runId": "stale-run-without-authority",
                "taskIdPresent": False,
                "executionGrantPresent": False,
                "response": stale_run_rtl,
            },
            "unrelatedTask": {
                "signaturePresent": False,
                "runIdPresent": False,
                "taskIdPresent": True,
                "taskId": "unrelated-task",
                "executionGrantPresent": False,
                "response": stale_task_rtl,
            },
            "fakeExpiredGrant": {
                "signaturePresent": False,
                "runIdPresent": False,
                "taskIdPresent": False,
                "executionGrantPresent": True,
                "executionGrant": {
                    "signature": "expired-untrusted-grant",
                    "expiresAtUnixMs": 0,
                },
                "response": stale_grant_rtl,
            },
            "callerTarget": {
                "signaturePresent": False,
                "runIdPresent": False,
                "taskIdPresent": False,
                "executionGrantPresent": False,
                "callerReturnTargetEnuM": caller_return_target,
                "response": caller_target_rtl,
            },
        }
        for probe_name, probe in rtl_probes.items():
            response = probe["response"]
            require_check(response.get("accepted") is True, (probe_name, response))
            require_check(response.get("mode") == "RTL", (probe_name, response))
        return_cruise = await check.wait_pose(
            lambda point: (
                math.dist(point[:2], home[:2]) <= 0.65 and abs(point[2] - attack_start[2]) <= 0.3
            ),
            35,
            "direct unsigned RTL returns to the configured home at cruise altitude",
        )
        check.phase = "rtl-home"
        rtl_home = await check.wait_pose(
            lambda point: math.dist(point, home) <= 0.25,
            35,
            "direct unsigned RTL reaches configured home and ground",
        )
        rtl_actuator_output = await check.wait_authorized_motor_output(
            actuator_sequence_before_rtl,
            (home[0], home[1], attack_start[2]),
        )
        await check.request(check.long(22, p7=0.8), 22)
        check.phase = "second-takeoff"
        await check.wait_pose(lambda point: point[2] >= home[2] + 0.55, 25, "second takeoff")
        await check.request(check.long(21), 21)
        check.phase = "land-at-home"
        await check.wait_pose(lambda point: math.dist(point, home) <= 0.25, 30, "LAND at home")
        check.phase = "settled-home-before-disarm"
        await check.disarm_after_settle(home)
        armed = False
        check.phase = "confirm-disarmed"
        final_state = await check.wait_state(
            lambda state: state.get("armed") is False,
            5,
            "disarmed heartbeat",
            fresh_fields=("armed",),
        )
        final_pose = await check.wait_pose(
            lambda point: math.dist(point, home) <= 0.25, 3, "disarmed on ground"
        )

        check.phase = "audit-completeness"
        await asyncio.sleep(0.4)
        audit_ids = {record.get("message_id") for record in check.audit_frames}
        if not {43, 44, 45, 51, 73, 76, 84} <= audit_ids:
            raise AssertionError(f"JetStream MAVLink ingress audit missing frames: {audit_ids}")
        return {
            "schema": "dvd.gazebo-mavlink-flight-check.v1",
            "passed": True,
            "callsign": callsign,
            "initialGazeboPose": home,
            "takeoffPose": takeoff,
            "positionTargetPose": moved,
            "missionEndPose": mission_end,
            "directNatsRtlProbe": {
                "boundary": "loopback local Gazebo test authority; not furia-core",
                "wrongCallsign": wrong_callsign,
                "unsupportedReturnTarget": unsupported_return_target,
                "outOfEnvelopePosition": out_of_envelope,
                "rtlRequests": rtl_probes,
                "callerReturnTargetEnuM": caller_return_target,
                "targetSource": "authority-captured home; caller return-target fields were ignored",
                "trajectoryEvidence": {
                    "gazeboPoseBeforeRtl": attack_start,
                    "gazeboCruiseReturnPose": return_cruise,
                    "gazeboHomePose": rtl_home,
                },
                "independentActuatorOutput": rtl_actuator_output,
                "runTaskGrantFieldsEnforcedByOperatorEnvelope": False,
                "impactLimit": "requires access to this run's local NATS operator subject; no Core authority claim",
            },
            "finalGazeboPose": final_pose,
            "finalMavlinkState": final_state,
            "prearmReadyBeforeFlight": initial_state.get("prearm_ready"),
            "prearmReadyAfterDisarm": final_state.get("prearm_ready"),
            "gazeboVsMavlinkAltitudeDeltaM": round(
                final_state.get("alt_m", 0.0) - final_pose[2], 3
            ),
            "auditedMessageIds": sorted(audit_ids),
            "auditFrameCount": len(check.audit_frames),
            "rawGazeboTelemetry": check.raw_telemetry,
            "validGazeboTelemetryReceived": check.valid_telemetry_received.is_set(),
            "telemetryRejection": getattr(check, "telemetry_rejection", None),
        }
    except Exception as failure:
        cleanup = {
            "attempted": armed and home is not None,
            "rtlConfirmed": False,
            "landConfirmed": False,
            "disarmConfirmed": False,
        }
        if armed and home is not None:
            try:
                await check.request(check.long(20), 20, wait=2)
                await check.wait_pose(
                    lambda point: math.dist(point, home) <= 0.25, 40, "failure cleanup RTL"
                )
                cleanup["rtlConfirmed"] = True
                await check.request(check.long(21), 21, wait=2)
                await check.wait_pose(
                    lambda point: math.dist(point, home) <= 0.25, 15, "failure cleanup LAND"
                )
                cleanup["landConfirmed"] = True
                await check.disarm_after_settle(home)
                disarmed_state = await check.wait_state(
                    lambda state: state.get("armed") is False,
                    5,
                    "failure cleanup disarmed heartbeat",
                    fresh_fields=("armed",),
                )
                cleanup["disarmConfirmed"] = disarmed_state.get("armed") is False
            except Exception as cleanup_error:
                cleanup["error"] = f"{type(cleanup_error).__name__}: {cleanup_error}"
                print(
                    f"Safety cleanup could not confirm LAND/DISARM: {cleanup_error}",
                    file=sys.stderr,
                )
        report_path = write_failed_report(run_directory, callsign, failure, cleanup, check)
        print(f"Gazebo MAVLink failure report: {report_path}", file=sys.stderr)
        raise
    finally:
        await check.close()


async def main() -> None:
    result = await run_check()
    run_directory = Path(os.environ["ANTOINE_GZ_RUN_DIR"])
    report_path = write_run_json(run_directory, Path("gazebo-mavlink-acceptance.json"), result)
    print(json.dumps(result, indent=2))
    print(f"Gazebo MAVLink acceptance report: {report_path}")


if __name__ == "__main__":
    asyncio.run(main())
