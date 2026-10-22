"""Failure evidence from the Gazebo acceptance checker stays machine-readable."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import math
import sys
import tempfile
import time
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
                pose_observation=lambda: {
                    "simulationTimeSeconds": 12.5,
                    "position": (0.1, -0.02, 0.09),
                },
                telemetry={"armed": False, "mode": "STABILIZE"},
                raw_telemetry={"identity": "drone-1", "control_authority": "safe-hold"},
                phase="land-at-home",
                home_pose=(0.0, 0.0, 0.09),
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
            self.assertEqual(report["phase"], "land-at-home")
            self.assertEqual(report["initialHomePose"], [0.0, 0.0, 0.09])
            self.assertEqual(report["lastGazeboPoseObservation"]["simulationTimeSeconds"], 12.5)
            self.assertFalse(report["lastMavlinkState"]["armed"])
            self.assertTrue(report["cleanup"]["disarmConfirmed"])
            self.assertEqual(report["auditFrameCount"], 2)

    def test_nats_telemetry_accepts_only_the_gazebo_profile_contract(self) -> None:
        checker = CHECKER.GazeboFlightCheck.__new__(CHECKER.GazeboFlightCheck)
        checker.callsign = "drone-1"
        checker.raw_telemetry = {"identity": "drone-1", "protocol": "antoine-gz"}
        checker.telemetry_rejection = None
        checker.valid_telemetry_received = asyncio.Event()

        async def deliver(payload: bytes) -> None:
            await checker.on_telemetry(types.SimpleNamespace(data=payload))

        valid = {
            "schema": "furia.telemetry",
            "version": "1.0.0",
            "identity": "drone-1",
            "protocol": "antoine-gz",
            "observedAtUnixMs": time.time_ns() // 1_000_000,
        }
        asyncio.run(deliver(json.dumps(valid).encode()))
        self.assertEqual(checker.raw_telemetry, valid)
        self.assertIsNone(checker.telemetry_rejection)
        self.assertTrue(checker.valid_telemetry_received.is_set())

        for invalid in (
            {**valid, "protocol": "antoine"},
            {**valid, "version": "2.0.0"},
            {**valid, "identity": "another-drone"},
            {**valid, "observedAtUnixMs": valid["observedAtUnixMs"] - 1_001},
            {**valid, "observedAtUnixMs": valid["observedAtUnixMs"] + 251},
            {**valid, "observedAtUnixMs": True},
            {key: value for key, value in valid.items() if key != "observedAtUnixMs"},
        ):
            with mock.patch.object(
                CHECKER.time,
                "time_ns",
                return_value=valid["observedAtUnixMs"] * 1_000_000,
            ):
                asyncio.run(deliver(json.dumps(invalid).encode()))
            self.assertEqual(checker.raw_telemetry, valid)
            self.assertIsNotNone(checker.telemetry_rejection)

        asyncio.run(deliver(b"not-json"))
        self.assertEqual(checker.raw_telemetry, valid)
        self.assertEqual(checker.telemetry_rejection, "telemetry is not valid JSON")

    def test_pose_observation_requires_fresh_finite_timestamped_vehicle_pose(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            pose_path = Path(directory) / "gazebo-pose.jsonl"
            checker = CHECKER.GazeboFlightCheck.__new__(CHECKER.GazeboFlightCheck)
            checker.pose_file = pose_path
            checker.agent = "isr"
            rows = [
                {
                    "header": {"stamp": {"sec": str(second), "nsec": "500000000"}},
                    "pose": [
                        {"name": "ground_plane", "position": {"x": 0, "y": 0, "z": 0}},
                        {"name": "isr", "position": {"x": 1, "y": 2, "z": 8}},
                    ],
                }
                for second in (4, 5)
            ]
            pose_path.write_text("".join(f"{json.dumps(row)}\n" for row in rows))
            observation = checker.pose_observation()
            self.assertEqual(observation["position"], (1.0, 2.0, 8.0))
            self.assertEqual(observation["simulationTimeSeconds"], 5.5)

            invalid = {
                **rows[-1],
                "pose": [{"name": "isr", "position": {"x": math.nan, "y": 0, "z": 8}}],
            }
            pose_path.write_text(json.dumps(invalid) + "\n")
            self.assertIsNone(checker.pose_observation())

    def test_wait_pose_requires_simulation_time_to_advance(self) -> None:
        checker = CHECKER.GazeboFlightCheck.__new__(CHECKER.GazeboFlightCheck)
        checker.pose_observation = mock.Mock(
            side_effect=[
                {"simulationTimeSeconds": 1.0, "position": (2.0, 0.0, 0.0)},
                {"simulationTimeSeconds": 1.0, "position": (2.0, 0.0, 0.0)},
                {"simulationTimeSeconds": 1.02, "position": (2.0, 0.0, 0.0)},
            ]
        )

        pose = asyncio.run(checker.wait_pose(lambda point: point[0] >= 1.0, 1.0, "target"))

        self.assertEqual(pose, (2.0, 0.0, 0.0))
        self.assertEqual(checker.pose_observation.call_count, 3)

    def test_wait_state_does_not_accept_a_cached_pre_command_state(self) -> None:
        checker = CHECKER.GazeboFlightCheck.__new__(CHECKER.GazeboFlightCheck)
        checker.telemetry = {"armed": True}
        checker.vehicle_state_updated_at = time.monotonic() - 10.0

        async def no_messages(_seconds):
            if False:
                yield None, None

        checker.recv_messages = no_messages
        with self.assertRaisesRegex(
            AssertionError, "timed out waiting for a fresh armed heartbeat"
        ):
            asyncio.run(
                checker.wait_state(
                    lambda state: state.get("armed") is True,
                    0.01,
                    "a fresh armed heartbeat",
                )
            )

    def test_wait_state_requires_the_field_specific_mavlink_message(self) -> None:
        checker = CHECKER.GazeboFlightCheck.__new__(CHECKER.GazeboFlightCheck)
        checker.telemetry = {"armed": True, "prearm_ready": True}
        checker.vehicle_state_updated_at = time.monotonic()
        checker.state_field_updated_at = {"prearm_ready": checker.vehicle_state_updated_at}

        async def no_messages(_seconds):
            if False:
                yield None, None

        checker.recv_messages = no_messages
        with self.assertRaisesRegex(
            AssertionError, "timed out waiting for a fresh armed heartbeat"
        ):
            asyncio.run(
                checker.wait_state(
                    lambda state: state.get("armed") is True,
                    0.01,
                    "a fresh armed heartbeat",
                    fresh_fields=("armed",),
                )
            )


if __name__ == "__main__":
    unittest.main()
