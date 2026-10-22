"""Verify a GCS-style MAVLink heartbeat/request appears in the bridge audit."""

from __future__ import annotations

import asyncio
import os
import socket
import time
import uuid

import nats
from flight_check_support import require_check
from nats.js.api import AckPolicy, ConsumerConfig, DeliverPolicy

from dvd_research.strict_json import loads as strict_json_loads


def x25(data: bytes, extra: int) -> int:
    checksum = 0xFFFF
    for byte in (*data, extra):
        temporary = byte ^ (checksum & 0xFF)
        temporary ^= (temporary << 4) & 0xFF
        checksum = (
            (checksum >> 8) ^ (temporary << 8) ^ (temporary << 3) ^ (temporary >> 4)
        ) & 0xFFFF
    return checksum


def frame(message_id: int, payload: bytes, crc_extra: int, sequence: int) -> bytes:
    header = bytes(
        (
            0xFD,
            len(payload),
            0,
            0,
            sequence,
            255,
            190,
            message_id & 0xFF,
            (message_id >> 8) & 0xFF,
            (message_id >> 16) & 0xFF,
        )
    )
    checksum = x25(header[1:] + payload, crc_extra)
    return header + payload + checksum.to_bytes(2, "little")


async def main() -> None:
    callsign = os.environ.get("MAVLINK_CALLSIGN", "qgc-live")
    stream_name = os.environ.get("MAVLINK_AUDIT_STREAM", "ANTOINE_MAVLINK")
    subject = f"furia.mavlink.command.{callsign}"
    client = await nats.connect(
        os.environ.get("NATS_URL", "nats://127.0.0.1:14224"),
        name="qgc-bridge-acceptance-check",
    )
    js = client.jetstream()
    consumer = f"qgc_check_{uuid.uuid4().hex[:12]}"
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        info = await js.stream_info(stream_name)
        await js.add_consumer(
            stream_name,
            config=ConsumerConfig(
                durable_name=consumer,
                deliver_policy=DeliverPolicy.BY_START_SEQUENCE,
                opt_start_seq=info.state.last_seq + 1,
                ack_policy=AckPolicy.EXPLICIT,
                inactive_threshold=60,
                filter_subject=subject,
            ),
        )
        subscription = await js.pull_subscribe(
            subject,
            durable=consumer,
            stream=stream_name,
        )

        sock.bind(("127.0.0.1", 0))
        sock.settimeout(0.5)
        peer = ("127.0.0.1", int(os.environ.get("QGC_BRIDGE_PORT", "14651")))
        heartbeat = bytearray(9)
        heartbeat[4] = 6  # MAV_TYPE_GCS
        heartbeat[5] = 8  # MAV_AUTOPILOT_INVALID
        heartbeat[7] = 4  # MAV_STATE_ACTIVE
        sock.sendto(frame(0, heartbeat, 50, 0), peer)

        deadline = time.monotonic() + 8
        observed: dict[int, dict] = {}
        while time.monotonic() < deadline and not {0, 66} <= observed.keys():
            try:
                messages = await subscription.fetch(8, timeout=1)
            except nats.errors.TimeoutError:
                continue
            for message in messages:
                row = strict_json_loads(message.data)
                await message.ack()
                if row.get("direction") == "gcs_to_antoine" and row.get("message_id") == 0:
                    # Request message 66 encodes a u16 stream ID, u16 message
                    # rate, and target system/component bytes.
                    request_payload = bytes((33, 0, 1, 1, 1, 1))
                    sock.sendto(frame(66, request_payload, 148, 1), peer)
                if row.get("direction") == "gcs_to_antoine" and row.get("message_id") in (0, 66):
                    observed[row["message_id"]] = row

        require_check(
            {0, 66} <= observed.keys(),
            "bridge audit did not contain both a GCS heartbeat and a targeted "
            f"REQUEST_DATA_STREAM; observed IDs={sorted(observed)}",
        )
        print(
            "Bridge audited GCS heartbeat and REQUEST_DATA_STREAM:",
            {message_id: row.get("source") for message_id, row in observed.items()},
        )
        await subscription.unsubscribe()
    finally:
        sock.close()
        try:
            await js.delete_consumer(stream_name, consumer)
        except nats.js.errors.NotFoundError:
            pass
        await client.drain()


if __name__ == "__main__":
    asyncio.run(main())
