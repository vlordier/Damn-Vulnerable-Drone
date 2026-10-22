"""Unit tests for the bounded MAVLink swarm telemetry probe."""

from __future__ import annotations

import io
import json
import socket
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from check_udp_loopback import local_udp_loopback_available
from mavlink_swarm_probe import (
    CRC_EXTRA,
    main,
    parse_datagram,
    summarize,
    x25_crc,
)


def v2_frame(
    message_id: int,
    system_id: int,
    payload: bytes,
    component_id: int = 1,
    signature: bytes = b"",
) -> bytes:
    header = bytes(
        [
            0xFD,
            len(payload),
            0x01 if signature else 0,
            0,
            0,
            system_id,
            component_id,
            message_id & 0xFF,
            (message_id >> 8) & 0xFF,
            (message_id >> 16) & 0xFF,
        ]
    )
    body = header + payload
    crc = x25_crc(body[1:])
    crc = x25_crc(bytes([CRC_EXTRA[message_id]]), crc)
    return body + bytes([crc & 0xFF, (crc >> 8) & 0xFF]) + signature


def v1_frame(message_id: int, system_id: int, payload: bytes, component_id: int = 1) -> bytes:
    header = bytes([0xFE, len(payload), 0, system_id, component_id, message_id])
    body = header + payload
    crc = x25_crc(body[1:])
    crc = x25_crc(bytes([CRC_EXTRA[message_id]]), crc)
    return body + bytes([crc & 0xFF, (crc >> 8) & 0xFF])


def vehicle(system_id: int, counts: dict[str, int] | None = None) -> dict:
    return {
        "callsign": f"drone-{system_id}",
        "expectedSystemId": system_id,
        "messageCounts": counts or {"HEARTBEAT": 1, "GPS_RAW_INT": 1, "GLOBAL_POSITION_INT": 1},
        "systemIdsObserved": [system_id],
        "invalidChecksumFrames": 0,
        "foreignSystemIdFrames": 0,
        "trailingBytes": 0,
    }


class X25CrcTests(unittest.TestCase):
    def test_mcrf4xx_check_value(self) -> None:
        self.assertEqual(x25_crc(b"123456789"), 0x6F91)


class ParseDatagramTests(unittest.TestCase):
    def test_parses_valid_v2_heartbeat(self) -> None:
        datagram = v2_frame(0, 3, bytes(9))
        frames, trailing = parse_datagram(datagram)
        self.assertEqual(trailing, 0)
        self.assertEqual(len(frames), 1)
        frame = frames[0]
        self.assertEqual(frame["version"], 2)
        self.assertEqual(frame["message_name"], "HEARTBEAT")
        self.assertEqual(frame["system_id"], 3)
        self.assertTrue(frame["checksumValid"])

    def test_parses_concatenated_v1_and_v2_frames(self) -> None:
        datagram = v2_frame(0, 1, bytes(9)) + v1_frame(24, 2, bytes(30))
        frames, trailing = parse_datagram(datagram)
        self.assertEqual(trailing, 0)
        self.assertEqual([frame["message_id"] for frame in frames], [0, 24])
        self.assertEqual([frame["version"] for frame in frames], [2, 1])
        self.assertTrue(all(frame["checksumValid"] for frame in frames))

    def test_parses_signed_v2_frame_checksum_before_signature(self) -> None:
        datagram = v2_frame(0, 4, bytes(9), signature=bytes(13))
        frames, trailing = parse_datagram(datagram)
        self.assertEqual(trailing, 0)
        self.assertEqual(len(frames), 1)
        self.assertTrue(frames[0]["signaturePresent"])
        self.assertTrue(frames[0]["checksumValid"])

    def test_detects_corrupt_checksum(self) -> None:
        datagram = bytearray(v2_frame(33, 1, bytes(28)))
        datagram[-1] ^= 0xFF
        frames, _ = parse_datagram(bytes(datagram))
        self.assertEqual(len(frames), 1)
        self.assertFalse(frames[0]["checksumValid"])

    def test_ignores_non_mavlink_noise(self) -> None:
        frames, trailing = parse_datagram(b"not-a-mavlink-packet")
        self.assertEqual(frames, [])
        self.assertEqual(trailing, len(b"not-a-mavlink-packet"))


class SummarizeTests(unittest.TestCase):
    def test_passes_when_every_vehicle_is_isolated(self) -> None:
        vehicles = {
            "drone-1": vehicle(1),
            "drone-2": vehicle(2),
        }
        gates, passed = summarize(vehicles)
        self.assertTrue(all(gates.values()))
        self.assertEqual(passed, ["drone-1", "drone-2"])

    def test_cross_talk_fails_the_run(self) -> None:
        vehicles = {
            "drone-1": vehicle(1),
            "drone-2": vehicle(2),
        }
        vehicles["drone-2"]["systemIdsObserved"] = [2, 1]
        vehicles["drone-2"]["foreignSystemIdFrames"] = 1
        gates, passed = summarize(vehicles)
        self.assertFalse(gates["noCrossVehicleFrames"])
        self.assertEqual(passed, ["drone-1"])

    def test_malformed_datagram_fails_the_vehicle(self) -> None:
        vehicles = {
            "drone-1": vehicle(1),
            "drone-2": vehicle(2),
        }
        vehicles["drone-1"]["trailingBytes"] = 3
        gates, passed = summarize(vehicles)
        self.assertFalse(gates["noMalformedDatagrams"])
        self.assertEqual(passed, ["drone-2"])

    def test_missing_message_type_fails_the_vehicle(self) -> None:
        vehicles = {
            "drone-1": vehicle(1, {"HEARTBEAT": 1}),
            "drone-2": vehicle(2),
        }
        _, passed = summarize(vehicles)
        self.assertEqual(passed, ["drone-2"])


def reserve_ports(count: int) -> list[int]:
    sockets = []
    try:
        for _ in range(count):
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.bind(("127.0.0.1", 0))
            sockets.append(sock)
        return [sock.getsockname()[1] for sock in sockets]
    finally:
        for sock in sockets:
            sock.close()


def send_frames(peer_ports: list[int], system_ids: list[int], count: int = 3) -> None:
    time.sleep(0.1)
    payloads = {
        0: bytes(9),
        24: bytes(30),
        33: bytes(28),
    }
    sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        for _ in range(count):
            for port, system_id in zip(peer_ports, system_ids, strict=True):
                for message_id, payload in payloads.items():
                    sender.sendto(v2_frame(message_id, system_id, payload), ("127.0.0.1", port))
            time.sleep(0.05)
    finally:
        sender.close()


class MainTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if not local_udp_loopback_available():
            raise unittest.SkipTest("local UDP delivery is unavailable on this host")

    def run_probe(self, extra_frames: list[tuple[int, int]] | None = None) -> tuple[int, dict]:
        ports = reserve_ports(4)
        bind_ports, peer_ports = ports[:2], ports[2:]
        system_ids = [11, 22]
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory)
            sender = threading.Thread(
                target=send_frames, args=(peer_ports, system_ids), daemon=True
            )
            sender.start()
            if extra_frames:

                def send_extra() -> None:
                    time.sleep(0.2)
                    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                    for port_index, system_id in extra_frames:
                        sock.sendto(
                            v2_frame(0, system_id, bytes(9)),
                            ("127.0.0.1", peer_ports[port_index]),
                        )
                    sock.close()

                threading.Thread(target=send_extra, daemon=True).start()
            argv = [
                "mavlink_swarm_probe.py",
                "--server",
                "nats://127.0.0.1:4222",
                "--run-dir",
                str(run_dir),
                "--callsigns",
                "drone-1,drone-2",
                "--bind-ports",
                f"{bind_ports[0]} {bind_ports[1]}",
                "--peer-ports",
                f"{peer_ports[0]} {peer_ports[1]}",
                "--system-ids",
                "11 22",
                "--duration",
                "2",
                "--max-frames",
                "30",
            ]
            with mock.patch.object(sys, "argv", argv), redirect_stdout(io.StringIO()):
                exit_code = main()
            record = json.loads((run_dir / "research-mavlink-swarm.json").read_text())
        sender.join(timeout=2)
        return exit_code, record

    def test_main_passes_isolated_telemetry(self) -> None:
        exit_code, record = self.run_probe()
        self.assertEqual(exit_code, 0)
        self.assertEqual(record["status"], "passed")
        self.assertEqual(record["vehiclesPassed"], ["drone-1", "drone-2"])
        for vehicle in record["vehicles"].values():
            self.assertGreaterEqual(vehicle["messageCounts"]["HEARTBEAT"], 1)
            self.assertGreaterEqual(vehicle["messageCounts"]["GLOBAL_POSITION_INT"], 1)

    def test_main_rejects_cross_vehicle_frames(self) -> None:
        exit_code, record = self.run_probe(extra_frames=[(0, 22)])
        self.assertEqual(exit_code, 2)
        self.assertEqual(record["status"], "inconclusive")
        self.assertFalse(record["gates"]["noCrossVehicleFrames"])


if __name__ == "__main__":
    unittest.main()
