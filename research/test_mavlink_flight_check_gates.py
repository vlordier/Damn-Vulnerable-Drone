from __future__ import annotations

import ast
import importlib.util
import inspect
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
