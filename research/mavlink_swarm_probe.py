#!/usr/bin/env python3
"""Receive bounded MAVLink telemetry from one bridge per swarm vehicle.

The Antoine MAVLink bridge is a telemetry fan-out: it subscribes to
``furia.telemetry.<callsign>`` and emits HEARTBEAT, GPS_RAW_INT, and
GLOBAL_POSITION_INT datagrams towards its configured QGC peer.  This probe
binds one UDP socket per callsign on that peer address, validates the frames
(including the MAVLink X25 checksum), and checks that each stream carries its
own vehicle's system ID without leaking into another vehicle's socket.

It never sends a MAVLink packet, so it cannot arm, disarm, or steer a vehicle.
It also does not prove that bridge commands control the Gazebo swarm.
"""

from __future__ import annotations

import argparse
import json
import selectors
import socket
import struct
import time
from pathlib import Path
from typing import Any

CONTRACT = "damn-vulnerable-drone.antoine-mavlink-swarm"
MAVLINK_V1_MAGIC = 0xFE
MAVLINK_V2_MAGIC = 0xFD
V2_SIGNATURE_LENGTH = 13
# CRC_EXTRA bytes for the messages the bridge emits.  Values come from the
# MAVLink common dialect; the bridge passes the same constants to its encoder.
CRC_EXTRA = {
    0: 50,  # HEARTBEAT
    24: 24,  # GPS_RAW_INT
    33: 104,  # GLOBAL_POSITION_INT
}
MESSAGE_NAMES = {
    0: "HEARTBEAT",
    24: "GPS_RAW_INT",
    33: "GLOBAL_POSITION_INT",
}
MIN_VEHICLES = 2
MAX_VEHICLES = 5
MAX_CALLSIGN_LENGTH = 32


def x25_crc(data: bytes, crc: int = 0xFFFF) -> int:
    """MAVLink's X25 checksum (CRC-16/MCRF4XX)."""
    for byte in data:
        tmp = byte ^ (crc & 0xFF)
        tmp ^= (tmp << 4) & 0xFF
        crc = ((crc >> 8) ^ (tmp << 8) ^ (tmp << 3) ^ (tmp >> 4)) & 0xFFFF
    return crc


def _parse_frame(buffer: bytes, offset: int) -> tuple[dict[str, Any] | None, int, str | None]:
    """Parse one frame at ``offset``.

    Returns the frame, the number of bytes consumed, and an optional error.
    """
    magic = buffer[offset]
    signature_length = 0
    if magic == MAVLINK_V2_MAGIC:
        if len(buffer) - offset < 12:
            return None, 0, "truncated-v2-header"
        payload_length = buffer[offset + 1]
        incompat_flags = buffer[offset + 2]
        signature_length = V2_SIGNATURE_LENGTH if incompat_flags & 0x01 else 0
        header_end = offset + 10
        message_id = buffer[offset + 7] | (buffer[offset + 8] << 8) | (buffer[offset + 9] << 16)
        system_id = buffer[offset + 5]
        component_id = buffer[offset + 6]
    else:
        if len(buffer) - offset < 8:
            return None, 0, "truncated-v1-header"
        payload_length = buffer[offset + 1]
        header_end = offset + 6
        message_id = buffer[offset + 5]
        system_id = buffer[offset + 4]
        component_id = buffer[offset + 3]
    # The checksum precedes the optional v2 signature block, so it is not
    # always the last two bytes of the frame.
    crc_offset = header_end + payload_length
    frame_length = crc_offset - offset + 2 + signature_length
    if len(buffer) - offset < frame_length:
        return None, 0, "truncated-frame"

    payload = buffer[header_end:crc_offset]
    expected_crc = buffer[crc_offset] | (buffer[crc_offset + 1] << 8)
    crc_extra = CRC_EXTRA.get(message_id)
    if crc_extra is None:
        checksum_status = "unsupported-message"
        checksum_valid = None
    else:
        calculated = x25_crc(buffer[offset + 1 : header_end + payload_length])
        calculated = x25_crc(bytes([crc_extra]), calculated)
        checksum_valid = calculated == expected_crc
        checksum_status = "valid" if checksum_valid else "invalid"

    return (
        {
            "magic": magic,
            "version": 2 if magic == MAVLINK_V2_MAGIC else 1,
            "message_id": message_id,
            "message_name": MESSAGE_NAMES.get(message_id, f"UNKNOWN_{message_id}"),
            "system_id": system_id,
            "component_id": component_id,
            "payload_length": payload_length,
            "signaturePresent": signature_length == V2_SIGNATURE_LENGTH,
            "checksumValid": checksum_valid,
            "checksumStatus": checksum_status,
            "payload": payload,
        },
        frame_length,
        None,
    )


def parse_datagram(buffer: bytes) -> tuple[list[dict[str, Any]], int]:
    """Return every complete frame in a datagram plus a trailing-byte count."""
    frames: list[dict[str, Any]] = []
    offset = 0
    parsed_end = 0
    while offset < len(buffer):
        magic = buffer[offset]
        if magic not in (MAVLINK_V1_MAGIC, MAVLINK_V2_MAGIC):
            offset += 1
            continue
        frame, consumed, error = _parse_frame(buffer, offset)
        if error is not None or frame is None:
            break
        frames.append(frame)
        offset += consumed
        parsed_end = offset
    return frames, len(buffer) - parsed_end


def _global_position(payload: bytes) -> dict[str, Any] | None:
    if len(payload) < 28:
        return None
    lat, lon, alt, relative_alt, vx, vy, vz, heading = struct.unpack_from("<iiiihhhH", payload, 4)
    return {
        "latitudeDeg": round(lat / 1e7, 7),
        "longitudeDeg": round(lon / 1e7, 7),
        "altitudeMslM": round(alt / 1000.0, 3),
        "relativeAltitudeM": round(relative_alt / 1000.0, 3),
        "velocityNedCms": [vx, vy, vz],
        "headingCdeg": heading,
    }


def _validate_lists(args: argparse.Namespace) -> list[str]:
    callsigns = args.callsigns.split(",")
    bind_ports = [int(value) for value in args.bind_ports.split()]
    peer_ports = [int(value) for value in args.peer_ports.split()]
    system_ids = [int(value) for value in args.system_ids.split()]
    if not MIN_VEHICLES <= len(callsigns) <= MAX_VEHICLES:
        raise SystemExit(f"callsigns must list {MIN_VEHICLES}..{MAX_VEHICLES} vehicles")
    if len(set(callsigns)) != len(callsigns):
        raise SystemExit("callsigns must be unique")
    if any(
        not callsign or len(callsign) > MAX_CALLSIGN_LENGTH or "," in callsign
        for callsign in callsigns
    ):
        raise SystemExit("each callsign must be a short, comma-free token")
    if not (len(bind_ports) == len(peer_ports) == len(system_ids) == len(callsigns)):
        raise SystemExit("bind ports, peer ports, and system IDs must match the callsigns")
    if len(set(system_ids)) != len(system_ids):
        raise SystemExit("system IDs must be unique per vehicle")
    if any(not 1 <= system_id <= 255 for system_id in system_ids):
        raise SystemExit("system IDs must be 1..255")
    for port in bind_ports + peer_ports:
        if not 1024 <= port <= 65535:
            raise SystemExit("ports must be unprivileged TCP/UDP ports")
    if set(bind_ports) & set(peer_ports):
        raise SystemExit("bind and peer ports must not overlap")
    return callsigns


def summarize(
    vehicles: dict[str, dict[str, Any]],
) -> tuple[dict[str, bool], list[str]]:
    """Annotate each vehicle with pass gates and return the run-level gates."""
    passed_vehicles = []
    for vehicle in vehicles.values():
        expected = vehicle["expectedSystemId"]
        vehicle["passGates"] = {
            "heartbeatReceived": vehicle["messageCounts"].get("HEARTBEAT", 0) >= 1,
            "gpsRawIntReceived": vehicle["messageCounts"].get("GPS_RAW_INT", 0) >= 1,
            "globalPositionIntReceived": vehicle["messageCounts"].get("GLOBAL_POSITION_INT", 0)
            >= 1,
            "onlyExpectedSystemId": vehicle["systemIdsObserved"] == [expected],
            "noInvalidChecksums": vehicle["invalidChecksumFrames"] == 0,
            "noForeignSystemIdFrames": vehicle["foreignSystemIdFrames"] == 0,
            "noTrailingBytes": vehicle["trailingBytes"] == 0,
        }
        if all(vehicle["passGates"].values()):
            passed_vehicles.append(vehicle["callsign"])

    unique_system_ids = len({vehicle["expectedSystemId"] for vehicle in vehicles.values()}) == len(
        vehicles
    )
    gates = {
        "allVehiclesReceivedTelemetry": len(passed_vehicles) == len(vehicles),
        "uniqueExpectedSystemIds": unique_system_ids,
        "noCrossVehicleFrames": all(
            vehicle["foreignSystemIdFrames"] == 0 for vehicle in vehicles.values()
        ),
        "noInvalidChecksums": all(
            vehicle["invalidChecksumFrames"] == 0 for vehicle in vehicles.values()
        ),
        "noMalformedDatagrams": all(vehicle["trailingBytes"] == 0 for vehicle in vehicles.values()),
    }
    return gates, passed_vehicles


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server", required=True, help="run-owned NATS URL (metadata only)")
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--callsigns", required=True)
    parser.add_argument("--bind-ports", required=True)
    parser.add_argument("--peer-ports", required=True)
    parser.add_argument("--system-ids", required=True)
    parser.add_argument("--duration", type=float, default=25.0)
    parser.add_argument("--max-frames", type=int, default=160)
    args = parser.parse_args()

    callsigns = _validate_lists(args)
    bind_ports = [int(value) for value in args.bind_ports.split()]
    peer_ports = [int(value) for value in args.peer_ports.split()]
    system_ids = [int(value) for value in args.system_ids.split()]
    if not 1.0 <= args.duration <= 60.0:
        raise SystemExit("duration must be 1..60 seconds")
    if not 1 <= args.max_frames <= 2000:
        raise SystemExit("frame cap must be 1..2000")

    vehicles: dict[str, dict[str, Any]] = {
        callsign: {
            "callsign": callsign,
            "expectedSystemId": system_ids[index],
            "bridgeBindPort": bind_ports[index],
            "probeListenPort": peer_ports[index],
            "validFrames": 0,
            "invalidChecksumFrames": 0,
            "unsupportedMessageFrames": 0,
            "trailingBytes": 0,
            "messageCounts": {},
            "systemIdsObserved": [],
            "componentIdsObserved": [],
            "foreignSystemIdFrames": 0,
            "firstGlobalPosition": None,
            "lastGlobalPosition": None,
        }
        for index, callsign in enumerate(callsigns)
    }

    started_at_ns = time.time_ns()
    sockets: dict[socket.socket, str] = {}
    selector = selectors.DefaultSelector()
    try:
        for callsign, peer_port in zip(callsigns, peer_ports, strict=True):
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("127.0.0.1", peer_port))
            sock.setblocking(False)
            sockets[sock] = callsign
            selector.register(sock, selectors.EVENT_READ)

        deadline = time.monotonic() + args.duration
        total_frames = 0
        while total_frames < args.max_frames:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            for key, _ in selector.select(min(0.2, remaining)):
                sock = key.fileobj
                callsign = sockets[sock]
                try:
                    datagram, _ = sock.recvfrom(65535)
                except BlockingIOError:
                    continue
                frames, trailing = parse_datagram(datagram)
                vehicle = vehicles[callsign]
                vehicle["trailingBytes"] += trailing
                for frame in frames:
                    total_frames += 1
                    if frame["checksumStatus"] == "invalid":
                        vehicle["invalidChecksumFrames"] += 1
                        continue
                    if frame["checksumStatus"] == "unsupported-message":
                        vehicle["unsupportedMessageFrames"] += 1
                        continue
                    vehicle["validFrames"] += 1
                    name = frame["message_name"]
                    vehicle["messageCounts"][name] = vehicle["messageCounts"].get(name, 0) + 1
                    if frame["system_id"] not in vehicle["systemIdsObserved"]:
                        vehicle["systemIdsObserved"].append(frame["system_id"])
                    if frame["component_id"] not in vehicle["componentIdsObserved"]:
                        vehicle["componentIdsObserved"].append(frame["component_id"])
                    if frame["system_id"] != vehicle["expectedSystemId"]:
                        vehicle["foreignSystemIdFrames"] += 1
                    if frame["message_id"] == 33:
                        position = _global_position(frame["payload"])
                        if position is not None:
                            if vehicle["firstGlobalPosition"] is None:
                                vehicle["firstGlobalPosition"] = position
                            vehicle["lastGlobalPosition"] = position
                    if total_frames >= args.max_frames:
                        break
                if total_frames >= args.max_frames:
                    break
    finally:
        selector.close()
        for sock in sockets:
            sock.close()

    gates, passed_vehicles = summarize(vehicles)
    status = "passed" if all(gates.values()) else "inconclusive"

    output = args.run_dir / "research-mavlink-swarm.json"
    record = {
        "contract": CONTRACT,
        "version": "1.0.0",
        "runId": args.run_dir.name,
        "natsServer": args.server,
        "probe": "read-only MAVLink UDP receive; one socket per callsign",
        "durationSeconds": args.duration,
        "maxFrames": args.max_frames,
        "vehicleCount": len(vehicles),
        "vehicles": vehicles,
        "gates": gates,
        "vehiclesPassed": passed_vehicles,
        "status": status,
        "startedAtUnixNs": started_at_ns,
        "completedAtUnixNs": time.time_ns(),
        "limits": (
            "Verifies per-vehicle MAVLink telemetry fan-out and system-ID isolation "
            "only. It sends no MAVLink packet, does not arm, disarm, or steer a "
            "vehicle, and does not show that bridge commands control the Gazebo swarm."
        ),
    }
    output.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    print(output)
    return 0 if status == "passed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
