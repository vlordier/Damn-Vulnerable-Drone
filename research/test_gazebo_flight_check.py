"""Failure evidence from the Gazebo acceptance checker stays machine-readable."""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

SCRIPT = Path(__file__).parent / "mavlink-nats-console/gazebo_flight_check.py"
STUB_FLIGHT_CHECK = types.ModuleType("flight_check")
STUB_FLIGHT_CHECK.FlightCheck = object
SPEC = importlib.util.spec_from_file_location("gazebo_flight_check_under_test", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
CHECKER = importlib.util.module_from_spec(SPEC)
with mock.patch.dict(sys.modules, {"flight_check": STUB_FLIGHT_CHECK}):
    SPEC.loader.exec_module(CHECKER)


class GazeboFailureReportTests(unittest.TestCase):
    def test_failure_report_preserves_failure_pose_state_and_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            report_path = Path(directory) / "nested/acceptance.json"
            check = types.SimpleNamespace(
                pose=lambda: (0.1, -0.02, 0.09),
                telemetry={"armed": False, "mode": "STABILIZE"},
                raw_telemetry={"identity": "drone-1", "control_authority": "safe-hold"},
                audit_frames=[{}, {}],
            )

            CHECKER.write_failed_report(
                report_path,
                "drone-1",
                AssertionError("timed out waiting for second takeoff"),
                {
                    "attempted": True,
                    "rtlConfirmed": True,
                    "landConfirmed": True,
                    "disarmConfirmed": True,
                },
                check,
            )

            report = json.loads(report_path.read_text())
            self.assertFalse(report["passed"])
            self.assertEqual(report["failure"]["type"], "AssertionError")
            self.assertEqual(report["lastGazeboPose"], [0.1, -0.02, 0.09])
            self.assertFalse(report["lastMavlinkState"]["armed"])
            self.assertTrue(report["cleanup"]["disarmConfirmed"])
            self.assertEqual(report["auditFrameCount"], 2)


if __name__ == "__main__":
    unittest.main()
