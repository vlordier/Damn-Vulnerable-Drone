"""Run a bounded local MAVLink/NATS ARM-DISARM integration check."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import socket
import struct
import time
import uuid

import nats
from flight_check_support import require_check
from nats.js.api import AckPolicy, ConsumerConfig, DeliverPolicy


def arm_value_from_audit(record: dict) -> float:
    frame = base64.b64decode(record["raw_base64"], validate=True)
    if len(frame) < 14 or frame[0] != 0xFD or int.from_bytes(frame[7:10], "little") != 76:
        raise AssertionError(
            f"audit record does not contain a MAVLink 2 COMMAND_LONG frame: {record}"
        )
    if len(frame) != frame[1] + 12:
        raise AssertionError(f"invalid MAVLink 2 frame length in audit record: {record}")
    command = int.from_bytes(frame[38:40], "little")
    if command != 400:  # MAV_CMD_COMPONENT_ARM_DISARM
        raise ValueError(f"not an ARM/DISARM command frame: {record}")
    return struct.unpack("<f", frame[10:14])[0]


async def main() -> None:
    nats_url = os.environ.get("MAVLINK_NATS_URL", "nats://127.0.0.1:14224")
    callsign = os.environ.get("MAVLINK_CALLSIGN", "qgc-live")
    control_addr = ("127.0.0.1", int(os.environ.get("MAVLINK_CONTROL_PORT", "14573")))
    telemetry_subject = f"furia.telemetry.{callsign}"
    client = await nats.connect(nats_url, name="mavlink-roundtrip-check")
    telemetry = await client.subscribe(telemetry_subject)
    await client.flush()

    async def next_json(subscription: nats.aio.subscription.Subscription) -> dict:
        message = await asyncio.wait_for(subscription.next_msg(), timeout=5)
        return json.loads(message.data)

    async def wait_telemetry_state(armed: bool) -> dict:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            state = await next_json(telemetry)
            if state.get("identity") == callsign and state.get("armed") is armed:
                return state
        raise TimeoutError(f"NATS telemetry did not report armed={armed}")

    before = await next_json(telemetry)
    require_check(before.get("identity") == callsign, before)
    print(f"NATS telemetry live: identity={callsign} thrust={before.get('actuator_thrust')}")
    js = client.jetstream()
    stream_name = os.environ.get("MAVLINK_AUDIT_STREAM", "ANTOINE_MAVLINK")
    initial_stream = await js.stream_info(stream_name)
    initial_sequence = initial_stream.state.last_seq
    audit_consumer_name = f"mavlink_check_{uuid.uuid4().hex[:12]}"
    await js.add_consumer(
        stream_name,
        config=ConsumerConfig(
            durable_name=audit_consumer_name,
            deliver_policy=DeliverPolicy.BY_START_SEQUENCE,
            opt_start_seq=initial_sequence + 1,
            ack_policy=AckPolicy.EXPLICIT,
            filter_subject=f"furia.mavlink.command.{callsign}",
            inactive_threshold=60,
        ),
    )
    audit = await js.pull_subscribe(
        f"furia.mavlink.command.{callsign}",
        durable=audit_consumer_name,
        stream=stream_name,
    )

    control = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    control.settimeout(7)

    def request(action: str) -> str:
        request_id = uuid.uuid4().hex[:12]
        control.sendto(f"{action} {request_id}".encode(), control_addr)
        response, _ = control.recvfrom(512)
        response_text = response.decode()
        expected_armed = action == "arm"
        expected_state = f"heartbeat armed={str(expected_armed).lower()}"
        if not response_text.startswith(f"ok {request_id} acked {action} {expected_state}"):
            raise RuntimeError(f"{action} failed: {response_text}")
        return response_text

    disarm_confirmed = False
    try:
        arm_result = await asyncio.to_thread(request, "arm")
        arm_state = await wait_telemetry_state(True)
        print(
            f"MAVLink ARM ACK: {arm_result}; "
            f"NATS telemetry armed={arm_state.get('armed')} mode={arm_state.get('mode')}"
        )

        disarm_result = await asyncio.to_thread(request, "disarm")
        disarm_confirmed = True
        disarm_state = await wait_telemetry_state(False)
        print(
            f"MAVLink DISARM ACK: {disarm_result}; "
            f"NATS telemetry armed={disarm_state.get('armed')} mode={disarm_state.get('mode')}"
        )

        # The bridge audits each raw COMMAND_LONG before publishing the live
        # actuator command and returning COMMAND_ACK.
        audit_records = []
        audit_sequences = []
        observed_arm_values = []
        deadline = time.monotonic() + 8
        while observed_arm_values != [1.0, 0.0] and time.monotonic() < deadline:
            try:
                audit_messages = await audit.fetch(16, timeout=1)
            except nats.errors.TimeoutError:
                continue
            for message in audit_messages:
                record = json.loads(message.data)
                await message.ack()
                if record.get("direction") != "gcs_to_antoine" or record.get("message_id") != 76:
                    continue
                try:
                    arm_value = arm_value_from_audit(record)
                except ValueError:
                    continue
                audit_records.append(record)
                audit_sequences.append(message.metadata.sequence.stream)
                observed_arm_values.append(arm_value)
        require_check(observed_arm_values == [1.0, 0.0], (observed_arm_values, audit_records))
        print(
            "JetStream audited ARM then DISARM COMMAND_LONG frames: "
            f"param1={observed_arm_values} stream_sequences={audit_sequences}"
        )

        deadline = time.monotonic() + 8
        final = before
        while time.monotonic() < deadline:
            final = await next_json(telemetry)
            if (
                final.get("ground_contact") is True
                and float(final.get("actuator_thrust", 1.0)) < 1e-8
                and abs(float(final.get("alt_m", 999.0))) < 1e-6
                and abs(float(final.get("vd_mps", 999.0))) < 1e-6
            ):
                break
        else:
            raise RuntimeError(f"SITL did not return to ground after DISARM: {final}")
        print(
            "SITL safe after DISARM: "
            f"ground_contact={final.get('ground_contact')} "
            f"thrust={final.get('actuator_thrust')} alt_m={final.get('alt_m')} "
            f"vd_mps={final.get('vd_mps')}"
        )
    finally:
        if not disarm_confirmed:
            try:
                result = await asyncio.to_thread(request, "disarm")
                print(f"Safety DISARM after failed check: {result}")
            except (OSError, RuntimeError, TimeoutError) as error:
                print(f"Safety DISARM attempt failed: {error}")
        control.close()
        await telemetry.unsubscribe()
        await audit.unsubscribe()
        try:
            await js.delete_consumer(stream_name, audit_consumer_name)
        except nats.js.errors.NotFoundError:
            pass
        await client.drain()


if __name__ == "__main__":
    asyncio.run(main())
