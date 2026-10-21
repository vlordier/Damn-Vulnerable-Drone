from __future__ import annotations

import unittest

from antoine_setpoint_rate_probe import (
    accepted_sequences,
    mutate,
    rejection_sequences,
    validate_limits,
    validate_server,
)


class SetpointRateProbeTests(unittest.TestCase):
    def test_server_must_be_explicit_ipv4_loopback(self) -> None:
        validate_server("nats://127.0.0.1:4222")
        for server in (
            "nats://localhost:4222",
            "nats://192.0.2.1:4222",
            "nats://user:pass@127.0.0.1:4222",
        ):
            with self.subTest(server=server), self.assertRaises(ValueError):
                validate_server(server)

    def test_hard_caps_limit_duration_rate_and_message_count(self) -> None:
        self.assertEqual(validate_limits(10, 2.0, 20), 20)
        self.assertEqual(validate_limits(20, 5.0, 100), 100)
        for values in ((21, 1.0, 100), (10, 5.01, 100), (1, 0.1, 10), (1, 1.0, 101)):
            with self.subTest(values=values), self.assertRaises(ValueError):
                validate_limits(*values)

    def test_mutation_changes_only_vehicle_binding_and_unique_sequence(self) -> None:
        source = {
            "contract": "furia.gzsitl.mission-setpoint",
            "version": "2.1.0",
            "vehicleId": "drone-2",
            "taskId": "authorized-task",
            "sequence": 10,
            "target_enu_m": [1.0, 2.0, 3.0],
        }
        result = mutate(source, "drone-1", "drone-2", 110)
        self.assertEqual(result["vehicleId"], "drone-1")
        self.assertEqual(result["sequence"], 110)
        self.assertEqual(result["taskId"], source["taskId"])
        self.assertEqual(result["target_enu_m"], source["target_enu_m"])
        self.assertEqual(source["vehicleId"], "drone-2")

    def test_rejections_correlate_exact_target_source_and_sequence(self) -> None:
        lines = [
            "ANTOINE_GZ_SETPOINT_REJECTED callsign=drone-2 reason=vehicle-or-sequence vehicle_id=drone-1 sequence=110 last_sequence=10",
            "ANTOINE_GZ_SETPOINT_REJECTED callsign=drone-2 reason=vehicle-or-sequence vehicle_id=drone-3 sequence=111 last_sequence=10",
            "ANTOINE_GZ_SETPOINT_RECEIVED callsign=drone-2 sequence=112 payload_sha256=abc",
        ]
        self.assertEqual(rejection_sequences(lines, "drone-2", "drone-1", [110, 111, 112]), {110})

    def test_controller_decisions_correlate_exact_injected_payload_hashes(self) -> None:
        hashes = {110: "attack-hash-110", 111: "attack-hash-111"}
        lines = [
            "ANTOINE_GZ_SETPOINT_RECEIVED callsign=drone-2 sequence=110 payload_sha256=authorized-hash",
            "ANTOINE_GZ_SETPOINT_RECEIVED callsign=drone-2 sequence=110 payload_sha256=attack-hash-110",
            "ANTOINE_GZ_SETPOINT_REJECTED callsign=drone-2 reason=current-grant-or-vigie-binding vehicle_id=drone-1 sequence=111 payload_sha256=attack-hash-111",
        ]
        self.assertEqual(accepted_sequences(lines, "drone-2", hashes), {110})
        self.assertEqual(
            rejection_sequences(lines, "drone-2", "drone-1", [110, 111], hashes), {111}
        )


if __name__ == "__main__":
    unittest.main()
