from __future__ import annotations

import ast
import asyncio
import importlib.util
import inspect
import json
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

CHECKER_DIR = Path(__file__).parent / "mavlink-nats-console"
SCRIPT = CHECKER_DIR / "flight_check.py"
SUPPORT = CHECKER_DIR / "flight_check_support.py"
sys.path.insert(0, str(CHECKER_DIR))
SPEC = importlib.util.spec_from_file_location("mavlink_flight_check_under_test", SCRIPT)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("could not load MAVLink flight acceptance checker")
FLIGHT_CHECK = importlib.util.module_from_spec(SPEC)
with mock.patch.dict(sys.modules, {"nats": types.ModuleType("nats")}):
    SPEC.loader.exec_module(FLIGHT_CHECK)
SUPPORT_SPEC = importlib.util.spec_from_file_location("flight_check_support_under_test", SUPPORT)
if SUPPORT_SPEC is None or SUPPORT_SPEC.loader is None:
    raise RuntimeError("could not load MAVLink acceptance gate support")
GATE_SUPPORT = importlib.util.module_from_spec(SUPPORT_SPEC)
SUPPORT_SPEC.loader.exec_module(GATE_SUPPORT)


class MavlinkFlightCheckGateTests(unittest.TestCase):
    def test_nats_callbacks_reject_non_object_payloads_and_keep_later_valid_data(self) -> None:
        self.assertIs(FLIGHT_CHECK.json.JSONDecodeError, json.JSONDecodeError)
        checker = FLIGHT_CHECK.FlightCheck.__new__(FLIGHT_CHECK.FlightCheck)
        checker.callsign = "drone-1"
        checker.telemetry = {}
        checker.telemetry_history = []
        checker.audit_frames = []
        checker.callback_rejections = []

        class Message:
            def __init__(self, data: bytes):
                self.data = data

        async def deliver() -> None:
            await checker.on_telemetry(Message(b"not-json"))
            await checker.on_telemetry(Message(b"[]"))
            await checker.on_audit(Message(b"{"))
            await checker.on_audit(Message(b"null"))
            await checker.on_audit(Message(b'{"message_id":true}'))
            await checker.on_telemetry(Message(b'{"identity":"drone-1","armed":true}'))
            await checker.on_audit(Message(b'{"message_id":76}'))

        asyncio.run(deliver())

        self.assertEqual(checker.telemetry, {"identity": "drone-1", "armed": True})
        self.assertEqual(checker.telemetry_history[0]["armed"], True)
        self.assertEqual(checker.audit_frames, [{"message_id": 76}])
        self.assertEqual(
            checker.callback_rejections,
            [
                "telemetry payload is not valid JSON",
                "telemetry payload is not a JSON object",
                "audit payload is not valid JSON",
                "audit payload is not a JSON object",
                "audit payload has no valid MAVLink message ID",
            ],
        )

    def test_acceptance_checker_has_no_optimization_removable_asserts(self) -> None:
        scripts = [
            CHECKER_DIR / name
            for name in (
                "flight_check.py",
                "gazebo_flight_check.py",
                "check_qgc_bridge.py",
                "check_roundtrip.py",
            )
        ]
        for script in scripts:
            with self.subTest(script=script.name):
                tree = ast.parse(script.read_text(encoding="utf-8"))
                self.assertFalse(any(isinstance(node, ast.Assert) for node in ast.walk(tree)))

    def test_explicit_gate_still_raises_when_compiled_with_optimization(self) -> None:
        source = inspect.getsource(GATE_SUPPORT.require_check)
        namespace: dict[str, object] = {}
        exec(compile(source, str(SCRIPT), "exec", optimize=2), namespace)

        with self.assertRaisesRegex(AssertionError, "acceptance gate failed"):
            namespace["require_check"](False, "acceptance gate failed")

    def test_explicit_gate_accepts_true_conditions(self) -> None:
        GATE_SUPPORT.require_check(True, "unused message")


if __name__ == "__main__":
    unittest.main()
