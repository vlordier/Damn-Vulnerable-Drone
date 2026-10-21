#!/usr/bin/env python3
"""Bounded MAVLink command/mission/setpoint check against an isolated local Antoine SITL."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

import nats

logging.getLogger("nats").setLevel(logging.ERROR)

ROOT = Path(__file__).resolve().parents[2]
BRIDGE = Path(
    os.environ.get(
        "ANTOINE_BRIDGE_DIR",
        str(ROOT / "research" / "antoine-mavlink-jetstream-bridge"),
    )
)
ANTOINE = Path(os.environ.get("ANTOINE_ROOT", str(Path.home() / "Work/antoine")))
TARGET = Path(os.environ.get("ANTOINE_FLIGHT_TARGET", "/tmp/antoine-flight-target"))
CARGO_TARGET = Path(os.environ.get("MAVLINK_BRIDGE_TARGET", "/tmp/dvd-antoine-bridge-target"))
sys.path.insert(0, str(ROOT / "research" / "mavlink-nats-console"))


def free_tcp() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def free_udp() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def run(*args: str, cwd: Path | None = None, env: dict[str, str] | None = None) -> None:
    subprocess.run(args, cwd=cwd, env=env, check=True)


class FlightCheck:
    def __init__(self, url: str, bind_port: int, bridge_port: int, callsign: str):
        self.url = url
        self.callsign = callsign
        self.sequence = 0
        from pymavlink.dialects.v20 import common

        self.parser = common.MAVLink(None)
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.socket.bind(("127.0.0.1", bind_port))
        self.socket.settimeout(0.5)
        self.bridge = ("127.0.0.1", bridge_port)
        self.frames: list[bytes] = []
        self.received_frames: list[str] = []
        self.audit_frames: list[dict] = []
        self.telemetry: dict = {}
        self.bridge_heartbeat = False
        self.telemetry_history: list[dict] = []
        self.nc = None
        self.telemetry_sub = None
        self.audit_sub = None

    def encode(self, msgid: int, payload: bytes) -> bytes:
        from pymavlink.dialects.v20 import common

        extras = {
            0: 50,
            11: 89,
            43: 132,
            44: 221,
            45: 232,
            47: 153,
            51: 196,
            73: 38,
            76: 152,
            77: 143,
            84: 143,
        }
        header = bytes((len(payload), 0, 0, self.sequence, 255, 190)) + msgid.to_bytes(3, "little")
        self.sequence = (self.sequence + 1) & 0xFF
        crc = common.x25crc(header + payload + bytes((extras[msgid],))).crc
        return b"\xfd" + header + payload + struct.pack("<H", crc)

    def heartbeat(self):
        return self.encode(0, bytes((0, 0, 0, 0, 6, 8, 0, 0, 3)))

    async def start(self):
        self.nc = await nats.connect(self.url)
        self.telemetry_sub = await self.nc.subscribe(
            f"furia.telemetry.{self.callsign}", cb=self.on_telemetry
        )
        self.audit_sub = await self.nc.subscribe(
            f"furia.mavlink.command.{self.callsign}", cb=self.on_audit
        )
        await self.nc.flush()
        for _ in range(50):
            self.send_heartbeat()
            async for _, message in self.recv_messages(0.05):
                if message.get_type() == "HEARTBEAT":
                    self.bridge_heartbeat = True
            if self.telemetry and self.bridge_heartbeat:
                break
            await asyncio.sleep(0.05)
        assert self.telemetry, "did not receive native Antoine NATS telemetry"
        assert self.bridge_heartbeat, "bridge did not return a MAVLink HEARTBEAT"

    async def on_telemetry(self, msg):
        value = json.loads(msg.data)
        if value.get("identity") == self.callsign:
            self.telemetry = value
            self.telemetry_history.append(
                {
                    key: value.get(key)
                    for key in (
                        "time_s",
                        "lat_deg",
                        "lon_deg",
                        "alt_m",
                        "vn_mps",
                        "ve_mps",
                        "vd_mps",
                        "armed",
                        "mode",
                        "ground_contact",
                    )
                }
            )
            self.telemetry_history = self.telemetry_history[-80:]

    async def on_audit(self, msg):
        self.audit_frames.append(json.loads(msg.data))

    def send_heartbeat(self):
        self.socket.sendto(self.heartbeat(), self.bridge)

    async def recv_messages(self, seconds=0.5):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            try:
                raw, _ = self.socket.recvfrom(4096)
                self.received_frames.append(raw.hex())
                self.received_frames = self.received_frames[-12:]
                try:
                    decoded = self.parser.parse_buffer(raw)
                    if not decoded:
                        continue
                    message = decoded[0]
                    header = None
                    self.frames.append(raw)
                    yield header, message
                except Exception:
                    continue
            except TimeoutError:
                await asyncio.sleep(0)

    async def request(self, packet: bytes, command: int | None = None, result_ok=True, wait=4):
        self.socket.sendto(packet, self.bridge)
        end = time.monotonic() + wait
        while time.monotonic() < end:
            async for _, message in self.recv_messages(0.25):
                if message.get_type() == "COMMAND_ACK" and (
                    command is None or message.command == command
                ):
                    if result_ok:
                        assert message.result == 0, (
                            f"COMMAND_ACK result {message.result} for command {command}"
                        )
                    else:
                        assert message.result != 0, f"unexpectedly accepted command {command}"
                    return message
        raise AssertionError(
            f"no COMMAND_ACK for command={command}; last telemetry={self.telemetry}"
        )

    async def wait_state(self, predicate, timeout_s: float, description: str):
        end = time.monotonic() + timeout_s
        while time.monotonic() < end:
            if predicate(self.telemetry):
                return dict(self.telemetry)
            await asyncio.sleep(0.05)
        raise AssertionError(f"timed out waiting for {description}: {self.telemetry}")

    async def direct_control(
        self,
        command: dict,
        *,
        request_id: str,
        identity: str | None = None,
        extra_fields: dict | None = None,
    ) -> dict:
        """Publish directly to native SITL, bypassing MAVLink bridge ingress."""
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
        assert response["requestIdMatched"], response
        return response

    def long(self, command: int, p1=0.0, p2=0.0, p7=0.0):
        return self.encode(76, struct.pack("<7fHBBB", p1, p2, 0, 0, 0, 0, p7, command, 1, 1, 0))

    def position(self, north: float, east: float, down: float):
        payload = struct.pack(
            "<I11fHBBB",
            int(time.monotonic() * 1000) & 0xFFFFFFFF,
            north,
            east,
            down,
            0,
            0,
            0,
            0,
            0,
            0,
            0,
            0,
            0x0DF8,
            1,
            1,
            1,
        )
        return self.encode(84, payload)

    async def mission_upload(self, waypoints):
        self.socket.sendto(self.encode(44, struct.pack("<HBB", len(waypoints), 1, 1)), self.bridge)
        for index, (north, east, down) in enumerate(waypoints):
            await self.wait_for_message(51, lambda msg, item=index: msg.seq == item)
            payload = bytearray(37)
            payload[16:20] = struct.pack("<i", round(north * 10000))
            payload[20:24] = struct.pack("<i", round(east * 10000))
            payload[24:28] = struct.pack("<f", down)
            payload[28:30] = struct.pack("<H", index)
            payload[30:32] = struct.pack("<H", 16)
            payload[32:34] = bytes((1, 1))
            payload[34:37] = bytes((1, int(index == 0), 1))
            self.socket.sendto(self.encode(73, bytes(payload)), self.bridge)
        await self.wait_for_message(47, lambda msg: msg.type == 0)

    async def mission_clear(self):
        self.socket.sendto(self.encode(45, bytes((1, 1, 0))), self.bridge)
        await self.wait_for_message(47, lambda msg: msg.type == 0)

    async def wait_for_message(self, msgid, predicate, timeout_s=4):
        end = time.monotonic() + timeout_s
        while time.monotonic() < end:
            async for _, msg in self.recv_messages(0.2):
                if msg.get_msgId() == msgid and predicate(msg):
                    return msg
        raise AssertionError(
            f"timed out waiting for MAVLink message {msgid}; recent UDP frames={self.received_frames}"
        )

    async def mission_list(self):
        self.socket.sendto(self.encode(43, bytes((1, 1, 0))), self.bridge)
        message = await self.wait_for_message(44, lambda _: True)
        points = []
        for index in range(message.count):
            self.socket.sendto(self.encode(51, struct.pack("<HBB", index, 1, 1)), self.bridge)
            item = await self.wait_for_message(73, lambda msg, item=index: msg.seq == item)
            if item.frame == 1:  # MAV_FRAME_LOCAL_NED
                points.append((item.x / 10000, item.y / 10000, item.z))
            elif item.frame in (3, 6):  # GLOBAL_RELATIVE_ALT and its INT alias
                home_lat, home_lon = 48.0002, 0.3564
                north = (item.x / 1e7 - home_lat) * 111_111.0
                east = (item.y / 1e7 - home_lon) * 111_111.0 * math.cos(math.radians(home_lat))
                points.append((north, east, -item.z))
            else:
                raise AssertionError(f"unsupported mission readback frame {item.frame}")
        return message.count, points

    async def close(self):
        if self.nc:
            await self.nc.drain()
        self.socket.close()


async def scenario(url: str, qgc_port: int, bridge_port: int, callsign: str, report: Path):
    check = FlightCheck(url, qgc_port, bridge_port, callsign)
    try:
        await check.start()
        assert await check.mission_list() == (0, [])
        await check.request(check.long(400, p1=1.0), 400)
        armed = await check.wait_state(lambda t: t.get("armed") is True, 2, "armed state")
        assert armed["ground_contact"] is True

        await check.request(check.long(22, p7=1.0), 22)
        await check.wait_state(lambda t: t.get("alt_m", 0) >= 0.75, 12, "takeoff above 0.75m")
        assert check.telemetry["mode"] == "GUIDED"
        await check.request(check.long(176, p1=1.0, p2=3.0), 176)
        await check.wait_state(lambda t: t.get("mode") == "HOLD", 2, "HOLD mode")
        await check.request(check.long(176, p1=1.0, p2=1.0), 176)
        await check.wait_state(lambda t: t.get("mode") == "GUIDED", 2, "GUIDED mode")

        movement_deadline = time.monotonic() + 1.5
        while time.monotonic() < movement_deadline:
            check.socket.sendto(check.position(1.0, 0.5, -1.0), check.bridge)
            await asyncio.sleep(0.1)
        await check.wait_state(
            lambda t: (
                t.get("lat_deg", 48.0002) > 48.000201 and t.get("lon_deg", 0.3564) > 0.3564003
            ),
            2,
            "north and east position movement",
        )

        # Upload two bounded LOCAL_NED waypoints using the MAVLink mission microservice.
        await check.mission_upload([[0.5, 0.0, -1.0], [0.0, 0.0, -0.4]])
        mission = await check.nc.request(
            f"furia.mavlink.control.{callsign}",
            json.dumps(
                {
                    "schema": "furia.mavlink.control",
                    "version": "1.0.0",
                    "identity": callsign,
                    "request_id": "flight-check-mission-query",
                    "type": "mission_query",
                }
            ).encode(),
            timeout=2,
        )
        mission_state = json.loads(mission.data)
        assert mission_state["missionUploaded"] is True
        expected_waypoints = [[0.5, 0.0, -1.0], [0.0, 0.0, -0.4]]
        assert all(
            math.isclose(actual, expected, abs_tol=1e-5)
            for actual_waypoint, expected_waypoint in zip(
                mission_state["waypoints"], expected_waypoints, strict=True
            )
            for actual, expected in zip(actual_waypoint, expected_waypoint, strict=True)
        ), mission_state["waypoints"]
        await check.request(check.long(300), 300)
        await check.wait_state(
            lambda t: t.get("mode") == "HOLD" and t.get("alt_m", 9) >= 0.2,
            20,
            "mission reached terminal HOLD waypoint",
        )
        mission_after_execution_reply = await check.nc.request(
            f"furia.mavlink.control.{callsign}",
            json.dumps(
                {
                    "schema": "furia.mavlink.control",
                    "version": "1.0.0",
                    "identity": callsign,
                    "request_id": "flight-check-mission-after-execution",
                    "type": "mission_query",
                }
            ).encode(),
            timeout=2,
        )
        mission_after_execution = json.loads(mission_after_execution_reply.data)
        assert mission_after_execution["accepted"] is True, mission_after_execution
        assert mission_after_execution["missionUploaded"] is True, mission_after_execution
        assert len(mission_after_execution["waypoints"]) == 2, mission_after_execution
        mission_count, listed = await check.mission_list()
        assert mission_count == 2, {
            "missionCount": mission_count,
            "nativeMissionAfterExecution": mission_after_execution,
        }
        assert all(
            math.isclose(actual, expected, abs_tol=1e-5)
            for actual_waypoint, expected_waypoint in zip(listed, expected_waypoints, strict=True)
            for actual, expected in zip(actual_waypoint, expected_waypoint, strict=True)
        ), listed

        await check.request(check.long(176, p1=1.0, p2=1.0), 176)
        await check.wait_state(lambda t: t.get("mode") == "GUIDED", 2, "GUIDED mode")
        reposition_deadline = time.monotonic() + 1.5
        while time.monotonic() < reposition_deadline:
            check.socket.sendto(check.position(1.0, 0.5, -1.0), check.bridge)
            await asyncio.sleep(0.1)
        offset = await check.wait_state(
            lambda t: (
                t.get("lat_deg", 48.0002) > 48.000201 and t.get("lon_deg", 0.3564) > 0.3564003
            ),
            3,
            "native telemetry at offset before direct RTL",
        )
        wrong_identity = await check.direct_control(
            {"type": "rtl"},
            request_id="direct-rtl-wrong-identity",
            identity=f"{callsign}-spoofed",
        )
        assert wrong_identity["accepted"] is False, wrong_identity
        unsigned_stale_run_rtl = await check.direct_control(
            {"type": "rtl"},
            request_id="direct-rtl-unsigned-wrong-run",
            extra_fields={"runId": "stale-run-without-authority"},
        )
        assert unsigned_stale_run_rtl["accepted"] is True, unsigned_stale_run_rtl
        assert unsigned_stale_run_rtl["mode"] == "RTL", unsigned_stale_run_rtl
        unsigned_target_override = await check.direct_control(
            {
                "type": "rtl",
                "payload": {"north_m": 2.0, "east_m": 2.0, "down_m": -2.0},
            },
            request_id="direct-rtl-unsigned-target-override",
        )
        assert unsigned_target_override["accepted"] is True, unsigned_target_override

        def horizontal_distance_from_home(telemetry: dict) -> float:
            return math.hypot(
                (telemetry.get("lat_deg", 90.0) - 48.0002) * 111_111.0,
                (telemetry.get("lon_deg", 180.0) - 0.3564)
                * 111_111.0
                * math.cos(math.radians(48.0002)),
            )

        returned = await check.wait_state(
            lambda t: t.get("mode") == "LAND" and horizontal_distance_from_home(t) < 0.25,
            12,
            "direct RTL reaches home and transitions to LAND",
        )
        await check.request(check.long(21), 21)
        await check.wait_state(
            lambda t: t.get("ground_contact") is True and t.get("armed") is False,
            15,
            "landed and disarmed",
        )
        await check.request(check.long(400, p1=0.0), 400)
        for _ in range(40):
            if len(check.audit_frames) >= 8:
                break
            await asyncio.sleep(0.05)
        audited_ids = {frame["message_id"] for frame in check.audit_frames}
        assert {43, 44, 51, 73, 76, 84} <= audited_ids, (
            f"JetStream-backed frame audit missing MAVLink IDs: {audited_ids}"
        )
        final = await check.wait_state(
            lambda t: (
                not t.get("armed")
                and t.get("ground_contact")
                and abs(t.get("vd_mps", 1)) < 0.05
                and t.get("actuator_thrust", 1) < 1e-8
            ),
            3,
            "safe final state",
        )
        report.write_text(
            json.dumps(
                {
                    "schema": "dvd.mavlink-flight-check.v1",
                    "callsign": callsign,
                    "passed": True,
                    "finalTelemetry": final,
                    "checked": [
                        "NATS telemetry",
                        "ARM ACK",
                        "takeoff ACK and altitude",
                        "HOLD, GUIDED, and RTL mode changes",
                        "LOCAL_NED movement",
                        "mission upload, readback, and protocol ACKs",
                        "mission execution",
                        "wrong-identity rejection, unsigned stale-run RTL, unsigned target override, and landing",
                        "JetStream MAVLink command audit",
                    ],
                    "directNatsRtlBoundary": {
                        "wrongIdentityAttempt": {
                            "requestIdMatched": wrong_identity["requestIdMatched"],
                            "rejected": wrong_identity["accepted"] is False,
                            "reason": wrong_identity.get("reason"),
                        },
                        "unsignedStaleRunAttempt": {
                            "requestIdMatched": unsigned_stale_run_rtl["requestIdMatched"],
                            "signaturePresent": False,
                            "staleRunIdPresent": True,
                            "accepted": unsigned_stale_run_rtl["accepted"],
                        },
                        "unsignedTargetOverrideAttempt": {
                            "requestIdMatched": unsigned_target_override["requestIdMatched"],
                            "signaturePresent": False,
                            "staleRunIdPresent": False,
                            "accepted": unsigned_target_override["accepted"],
                            "requestedTarget": {
                                "north_m": 2.0,
                                "east_m": 2.0,
                                "down_m": -2.0,
                            },
                        },
                        "staleGrantAttempt": {
                            "tested": False,
                            "reason": "the native MAVLink control request has no grant field or grant verifier",
                        },
                        "positionBefore": {
                            "lat_deg": offset.get("lat_deg"),
                            "lon_deg": offset.get("lon_deg"),
                        },
                        "positionAfter": {
                            "lat_deg": returned.get("lat_deg"),
                            "lon_deg": returned.get("lon_deg"),
                        },
                        "homeDistanceAfterM": round(horizontal_distance_from_home(returned), 3),
                        "evidenceLimit": "native SITL telemetry is not independent Gazebo truth",
                    },
                    "independentFlightEvidence": {
                        "trajectory": {
                            "status": "unavailable",
                            "source": "native Antoine SITL telemetry",
                            "independent": False,
                            "reason": "this runner has no separate Gazebo pose source",
                        },
                        "actuator": {
                            "status": "unavailable",
                            "source": "actuator_thrust field in furia.telemetry",
                            "independent": False,
                            "reason": "the actuator value is emitted by the same native plant telemetry path",
                        },
                    },
                    "nativeSITLFinalState": {
                        "status": "observed",
                        "source": "furia.telemetry",
                        "groundContact": final["ground_contact"],
                        "armed": final["armed"],
                        "actuatorThrust": final["actuator_thrust"],
                    },
                },
                indent=2,
            )
            + "\n"
        )
    except BaseException:
        try:
            await check.request(check.long(21), 21, wait=1)
            await check.wait_state(lambda t: t.get("ground_contact") is True, 5, "cleanup landing")
        except BaseException:
            pass
        try:
            await check.request(check.long(400, p1=0.0), 400, wait=1)
        except BaseException:
            pass
        report.write_text(
            json.dumps(
                {
                    "schema": "dvd.mavlink-flight-check.v1",
                    "callsign": callsign,
                    "passed": False,
                    "lastTelemetry": check.telemetry,
                    "telemetryHistory": check.telemetry_history,
                },
                indent=2,
            )
            + "\n"
        )
        raise
    finally:
        await check.close()


async def main():
    report = Path(os.environ.get("MAVLINK_FLIGHT_REPORT", "/tmp/dvd-mavlink-flight-check.json"))
    port = free_tcp()
    qgc_port = free_udp()
    bridge_port = free_udp()
    while bridge_port == qgc_port:
        bridge_port = free_udp()
    callsign = f"mavflight-{uuid.uuid4().hex[:8]}"
    with tempfile.TemporaryDirectory(prefix="dvd-mavlink-flight-") as temporary:
        data = Path(temporary)
        server = subprocess.Popen(
            ["nats-server", "-js", "-a", "127.0.0.1", "-p", str(port), "-sd", str(data)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        children = []
        try:
            url = f"nats://127.0.0.1:{port}"
            server_ready = False
            for _ in range(100):
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                        server_ready = True
                    break
                except OSError:
                    await asyncio.sleep(0.05)
            if not server_ready:
                raise TimeoutError(f"temporary NATS server did not listen on 127.0.0.1:{port}")
            env = dict(
                os.environ,
                RUSTUP_TOOLCHAIN=os.environ.get("RUSTUP_TOOLCHAIN", "1.99.1"),
                CARGO_TARGET_DIR=str(TARGET),
            )
            run(
                "cargo",
                "build",
                "--quiet",
                "--features",
                "nats",
                "--bin",
                "antoine_sitl_nats",
                cwd=ANTOINE,
                env=env,
            )
            bridge_env = dict(
                env,
                CARGO_TARGET_DIR=str(CARGO_TARGET),
                RUST_LOG="antoine_mavlink_jetstream_bridge=debug",
            )
            run(
                "cargo",
                "build",
                "--quiet",
                "--locked",
                "--manifest-path",
                str(BRIDGE / "Cargo.toml"),
                cwd=BRIDGE,
                env=bridge_env,
            )
            sitl = subprocess.Popen(
                [
                    str(TARGET / "debug/antoine_sitl_nats"),
                    "--callsign",
                    callsign,
                    "--nats-url",
                    url,
                    "--plant",
                    "antoine",
                    "--enable-mavlink-control",
                    "--thrust",
                    "0",
                ],
                stdout=(data / "sitl.log").open("w"),
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            children.append(sitl)
            sitl_log = data / "sitl.log"
            bridge = subprocess.Popen(
                [
                    str(CARGO_TARGET / "debug/antoine-mavlink-jetstream-bridge"),
                    "--mode",
                    "antoine-nats",
                    "--callsign",
                    callsign,
                    "--nats-url",
                    url,
                    "--qgc-bind",
                    f"127.0.0.1:{bridge_port}",
                    "--qgc-peer",
                    f"127.0.0.1:{qgc_port}",
                    "--system-id",
                    "1",
                ],
                stdout=(data / "bridge.log").open("w"),
                stderr=subprocess.STDOUT,
                env=bridge_env,
                start_new_session=True,
            )
            children.append(bridge)
            bridge_log = data / "bridge.log"
            try:
                await scenario(url, qgc_port, bridge_port, callsign, report)
            except BaseException:
                if sitl_log.exists():
                    report.with_suffix(".sitl.log").write_text(sitl_log.read_text())
                if bridge_log.exists():
                    report.with_suffix(".bridge.log").write_text(bridge_log.read_text())
                raise
        finally:
            for child in reversed(children):
                if child.poll() is None:
                    os.killpg(child.pid, signal.SIGTERM)
                    try:
                        child.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        os.killpg(child.pid, signal.SIGKILL)
            if server.poll() is None:
                os.killpg(server.pid, signal.SIGTERM)
                try:
                    server.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    os.killpg(server.pid, signal.SIGKILL)


if __name__ == "__main__":
    asyncio.run(main())
