"""Failure evidence from the Gazebo acceptance checker stays machine-readable."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import math
import subprocess
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest import mock

SCRIPT = Path(__file__).parent / "mavlink-nats-console/gazebo_flight_check.py"
sys.path.insert(0, str(SCRIPT.parent))
STUB_FLIGHT_CHECK = types.ModuleType("flight_check")
STUB_FLIGHT_CHECK.FlightCheck = object
SPEC = importlib.util.spec_from_file_location("gazebo_flight_check_under_test", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
CHECKER = importlib.util.module_from_spec(SPEC)
with mock.patch.dict(sys.modules, {"flight_check": STUB_FLIGHT_CHECK}):
    SPEC.loader.exec_module(CHECKER)


class GazeboFailureReportTests(unittest.TestCase):
    def test_standalone_target_selects_run_owned_flight_proof_paths(self) -> None:
        makefile = SCRIPT.parents[2] / "Makefile"
        content = makefile.read_text(encoding="utf-8")
        self.assertIn("/flight-proof/gazebo-pose.jsonl", content)
        self.assertIn('ANTOINE_GZ_RUN_DIR="$$run_dir"', content)
        runner = SCRIPT.parents[1] / "run-antoine-gazebo-mavlink.sh"
        runner_text = runner.read_text(encoding="utf-8")
        self.assertIn('ANTOINE_GZ_RUN_DIR="${RUN_DIR}"', runner_text)
        self.assertIn('RUN_ID="$(basename "${RUN_DIR}")"', runner_text)
        self.assertGreaterEqual(runner_text.count('ANTOINE_GZ_FLIGHT_RUN_ID="${RUN_ID}"'), 2)

    def test_antoine_local_authority_patch_binds_runtime_world_digest(self) -> None:
        patch = SCRIPT.parents[1] / "patches/antoine-gazebo-battery-drain.patch"
        self.assertIn(
            '+    --world-digest "sha256:$WORLD_SHA256"',
            patch.read_text(encoding="utf-8"),
        )
        self.assertIn('+RUN_ID="${ANTOINE_GZ_FLIGHT_RUN_ID:-${RUN_DIR##*/}}"', patch.read_text())
        self.assertIn('+  FURIA_SITL_RUN_ID="$RUN_ID"', patch.read_text())

    def test_make_patch_target_applies_missing_world_binding_idempotently(self) -> None:
        repo = SCRIPT.parents[2]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "antoine"
            (root / ".git").mkdir(parents=True)
            rust = root / "sitl/antoine-sitl/src/bin/antoine_sitl_nats.rs"
            rust.parent.mkdir(parents=True)
            rust.write_text(
                "fn battery_after_drain() {}\n"
                "battery_pct = battery_after_drain(battery_pct, battery_drain_pct_per_s, dt)\n"
                "fn gazebo_plant_honors_configured_battery_drain_rate() {}\n",
                encoding="utf-8",
            )
            script = root / "scripts/gz_flight_proof.sh"
            script.parent.mkdir(parents=True)
            lines = ["# fixture"] * 730
            lines[382] = 'RUN_ID="${RUN_DIR##*/}"'
            lines[383] = ""
            lines[384] = (
                "# Derive the Ed25519 public keys the run pins. `uv run --with pynacl` keeps this"
            )
            lines[625] = (
                '  FURIA_GZ_APPLIER_REQUIRE_LIFECYCLE="$APPLIER_LIFECYCLE_REQUIRED" ' + chr(92)
            )
            lines[626] = (
                '  FURIA_SITL_RUN_ID="${RUN_DIR##*/}" GZ_SITL_WORLD_SHA256="sha256:$WORLD_SHA256" '
                + chr(92)
            )
            lines[627] = (
                """  FURIA_PRIME_ENU_FRAME_ID="$FRAME_ID" "$DVD_APPLIER_BIN" --agents "$AGENTS_CSV" --verbose' """
                + chr(92)
            )
            lines[722] = '  start_owned "$BIN_DIR/antoine_gz_local_authority" ' + chr(92)
            lines[723] = (
                '    --callsign "$callsign" --nats-url "$NATS_URL" --world-frame-id "$FRAME_ID" '
                + chr(92)
            )
            lines[724] = (
                '    --waypoints "$waypoints" --arrival-radius-m "$ARRIVAL_RADIUS_M" ' + chr(92)
            )
            script.write_text("\n".join(lines) + "\n", encoding="utf-8")
            subprocess.run(["git", "init", "--quiet", str(root)], check=True)

            command = ["make", "antoine-gz-battery-patch", f"ANTOINE_ROOT={root}"]
            first = subprocess.run(command, cwd=repo, capture_output=True, text=True, check=False)
            self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
            self.assertIn('--world-digest "sha256:$WORLD_SHA256"', script.read_text())
            self.assertIn(
                'RUN_ID="${ANTOINE_GZ_FLIGHT_RUN_ID:-${RUN_DIR##*/}}"', script.read_text()
            )
            self.assertIn('FURIA_SITL_RUN_ID="$RUN_ID"', script.read_text())

            second = subprocess.run(command, cwd=repo, capture_output=True, text=True, check=False)
            self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
            self.assertIn("runtime world-binding fixes are present", second.stdout)

    def test_gazebo_timestamp_rejects_out_of_range_and_unbounded_values(self) -> None:
        timestamp = CHECKER.gazebo_timestamp_seconds
        self.assertEqual(timestamp({"header": {"stamp": {"sec": "12", "nsec": "5"}}}), 12.000000005)
        for value in (
            {"header": {"stamp": {"sec": 1 << 63, "nsec": 0}}},
            {"header": {"stamp": {"sec": "9" * 20, "nsec": 0}}},
            {"header": {"stamp": {"sec": 1, "nsec": "9" * 10}}},
        ):
            with self.subTest(value=value):
                self.assertIsNone(timestamp(value))

    def test_failure_report_preserves_failure_pose_state_and_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_directory = Path(directory) / "run"
            run_directory.mkdir()
            check = types.SimpleNamespace(
                run_directory=run_directory,
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

            report_path = CHECKER.write_failed_report(
                run_directory,
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

    def test_failure_report_refuses_linked_run_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            outside = root / "outside"
            outside.mkdir()
            run_alias = root / "run"
            run_alias.symlink_to(outside, target_is_directory=True)
            check = types.SimpleNamespace(
                pose=lambda: None,
                pose_observation=lambda: None,
                telemetry={},
                raw_telemetry={},
                phase="connecting",
                home_pose=None,
                audit_frames=[],
                valid_telemetry_received=asyncio.Event(),
            )
            with self.assertRaisesRegex(ValueError, "run directory is a symlink"):
                CHECKER.write_failed_report(run_alias, "drone-1", RuntimeError("failed"), {}, check)
            self.assertEqual(list(outside.iterdir()), [])

    def test_failure_report_refuses_linked_report_destination(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_directory = Path(directory) / "run"
            run_directory.mkdir()
            outside = Path(directory) / "external-report.json"
            outside.write_text("preserve", encoding="utf-8")
            (run_directory / "gazebo-mavlink-acceptance.json").symlink_to(outside)
            check = types.SimpleNamespace(
                pose=lambda: None,
                pose_observation=lambda: None,
                telemetry={},
                raw_telemetry={},
                phase="connecting",
                home_pose=None,
                audit_frames=[],
                valid_telemetry_received=asyncio.Event(),
            )
            with self.assertRaisesRegex(ValueError, "destination is a symlink"):
                CHECKER.write_failed_report(
                    run_directory, "drone-1", RuntimeError("failed"), {}, check
                )
            self.assertEqual(outside.read_text(encoding="utf-8"), "preserve")

    def test_nats_telemetry_accepts_only_the_gazebo_profile_contract(self) -> None:
        checker = CHECKER.GazeboFlightCheck.__new__(CHECKER.GazeboFlightCheck)
        checker.callsign = "drone-1"
        checker.run_id = "run-1"
        checker.raw_telemetry = {"identity": "drone-1", "protocol": "antoine-gz"}
        checker.telemetry_rejection = None
        checker.valid_telemetry_received = asyncio.Event()
        checker.last_observed_at_ms = None

        async def deliver(payload: bytes) -> None:
            await checker.on_telemetry(types.SimpleNamespace(data=payload))

        valid = {
            "schema": "furia.telemetry",
            "version": "1.0.0",
            "identity": "drone-1",
            "protocol": "antoine-gz",
            "runId": "run-1",
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
            {**valid, "runId": "another-run"},
            {key: value for key, value in valid.items() if key != "runId"},
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

        for replayed_ms in (
            valid["observedAtUnixMs"],
            valid["observedAtUnixMs"] - 1,
        ):
            replayed = {**valid, "observedAtUnixMs": replayed_ms}
            with mock.patch.object(
                CHECKER.time,
                "time_ns",
                return_value=valid["observedAtUnixMs"] * 1_000_000,
            ):
                asyncio.run(deliver(json.dumps(replayed).encode()))
            self.assertEqual(checker.raw_telemetry, valid)
            self.assertEqual(
                checker.telemetry_rejection,
                "Gazebo telemetry observation timestamp did not advance",
            )

        advanced = {**valid, "observedAtUnixMs": valid["observedAtUnixMs"] + 1}
        with mock.patch.object(
            CHECKER.time,
            "time_ns",
            return_value=valid["observedAtUnixMs"] * 1_000_000,
        ):
            asyncio.run(deliver(json.dumps(advanced).encode()))
        self.assertEqual(checker.raw_telemetry, advanced)
        self.assertIsNone(checker.telemetry_rejection)

        asyncio.run(deliver(b"not-json"))
        self.assertEqual(checker.raw_telemetry, advanced)
        self.assertEqual(checker.telemetry_rejection, "telemetry is not valid JSON")

    def test_lost_arm_ack_still_attempts_failure_cleanup(self) -> None:
        cleanup_reports: list[dict] = []
        failure_to_raise: BaseException = AssertionError(
            "ARM ACK lost after command may have applied"
        )

        class FakeFlightCheck:
            def __init__(self, *_args):
                self.callsign = "drone-1"
                self.run_id = "run-1"
                self.telemetry = {"armed": False, "prearm_ready": True}
                self.valid_telemetry_received = asyncio.Event()
                self.audit_frames = []
                self.phase = "connecting"
                self.home_pose = None
                self.raw_telemetry = {}
                self.telemetry_rejection = None
                self.requests = []
                self.arm_command_sent = False
                self.failure = failure_to_raise

            async def start(self) -> None:
                self.valid_telemetry_received.set()

            async def wait_state(self, _predicate, _timeout, description, **kwargs) -> dict:
                if description == "failure cleanup disarmed heartbeat":
                    self.assert_fresh_disarm_wait(kwargs)
                return dict(self.telemetry)

            @staticmethod
            def assert_fresh_disarm_wait(kwargs) -> None:
                if kwargs.get("fresh_fields") != ("armed",):
                    raise AssertionError("cleanup must require a fresh armed heartbeat")

            async def wait_pose(self, *_args, **_kwargs) -> tuple[float, float, float]:
                if _args and _args[-1] == "failure cleanup RTL":
                    raise AssertionError("RTL pose confirmation lost")
                return (0.0, 0.0, 0.0)

            async def mission_clear(self) -> None:
                return None

            async def mission_list(self) -> tuple[int, list]:
                return (0, [])

            def long(self, command: int, **_kwargs) -> bytes:
                return str(command).encode()

            async def request(self, _packet: bytes, command: int, **_kwargs) -> None:
                self.requests.append(command)
                if command == 400 and not self.arm_command_sent:
                    self.arm_command_sent = True
                    self.telemetry["armed"] = True
                    raise self.failure

            async def disarm_after_settle(self, _home) -> None:
                self.telemetry["armed"] = False

            def pose(self):
                return (0.0, 0.0, 0.0)

            def pose_observation(self):
                return {
                    "simulationTimeSeconds": 1.0,
                    "position": (0.0, 0.0, 0.0),
                }

            async def close(self) -> None:
                return None

        def record_failed_report(_run_dir, _callsign, _failure, cleanup, _check):
            cleanup_reports.append(cleanup)
            return Path("failed-report.json")

        environment = {
            "ANTOINE_GZ_POSE_FILE": "/tmp/run/flight-proof/gazebo-pose.jsonl",
            "ANTOINE_GZ_RUN_DIR": "/tmp/run",
        }
        with (
            mock.patch.dict(CHECKER.os.environ, environment),
            mock.patch.object(CHECKER, "GazeboFlightCheck", FakeFlightCheck),
            mock.patch.object(CHECKER, "write_failed_report", side_effect=record_failed_report),
            mock.patch("builtins.print"),
        ):
            for failure in (
                AssertionError("ARM ACK lost after command may have applied"),
                asyncio.CancelledError(),
            ):
                with self.subTest(failure=type(failure).__name__):
                    failure_to_raise = failure
                    with self.assertRaises(type(failure)):
                        asyncio.run(CHECKER.run_check())

        expected_cleanup = {
            "attempted": True,
            "rtlConfirmed": False,
            "landConfirmed": True,
            "disarmConfirmed": True,
            "errors": {"rtl": "AssertionError: RTL pose confirmation lost"},
        }
        self.assertEqual(cleanup_reports, [expected_cleanup, expected_cleanup])

    def test_pose_observation_requires_fresh_finite_timestamped_vehicle_pose(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_directory = Path(directory)
            pose_path = run_directory / "flight-proof" / "gazebo-pose.jsonl"
            pose_path.parent.mkdir(parents=True)
            checker = CHECKER.GazeboFlightCheck.__new__(CHECKER.GazeboFlightCheck)
            checker.run_directory = run_directory
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

            invalid_positions = (
                {"x": math.nan, "y": 0, "z": 8},
                {"x": math.inf, "y": 0, "z": 8},
                {"x": 1 << 10_000, "y": 0, "z": 8},
                {"x": True, "y": 0, "z": 8},
            )
            for position in invalid_positions:
                invalid = {
                    **rows[-1],
                    "pose": [{"name": "isr", "position": position}],
                }
                pose_path.write_text(json.dumps(invalid) + "\n")
                with self.subTest(position=position):
                    self.assertIsNone(checker.pose_observation())

            external_pose = Path(directory) / "external-pose.jsonl"
            external_pose.write_text(json.dumps(rows[-1]) + "\n", encoding="utf-8")
            pose_path.unlink()
            pose_path.symlink_to(external_pose)
            self.assertIsNone(checker.pose_observation())

    def test_actuator_audit_ignores_symlinked_run_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_directory = Path(directory) / "run"
            audit_directory = run_directory / "flight-proof" / "applier-neutral"
            audit_directory.mkdir(parents=True)
            outside = Path(directory) / "external-actuator-output.jsonl"
            outside.write_text(
                json.dumps(
                    {
                        "contract": CHECKER.ACTUATOR_OUTPUT_CONTRACT,
                        "version": CHECKER.ACTUATOR_OUTPUT_VERSION,
                        "runId": run_directory.name,
                        "vehicleId": "drone-1",
                        "actuatorKind": "motors",
                        "sequence": 99,
                        "neutral": False,
                        "executionGrant": {
                            "contract": "furia.prime.execution-grant",
                            "targetEnuMm": [0, 0, 1000],
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            audit_path = audit_directory / "actuator-output.jsonl"
            audit_path.write_text(outside.read_text(encoding="utf-8"), encoding="utf-8")
            checker = CHECKER.GazeboFlightCheck.__new__(CHECKER.GazeboFlightCheck)
            checker.run_directory = run_directory
            checker.run_id = run_directory.name
            checker.callsign = "drone-1"
            self.assertEqual(checker.actuator_audit()[0]["sequence"], 99)

            audit_path.unlink()
            audit_path.symlink_to(outside)
            self.assertEqual(checker.actuator_audit(), [])

            audit_path.unlink()
            audit_directory.rmdir()
            audit_directory.symlink_to(outside.parent, target_is_directory=True)
            self.assertEqual(checker.actuator_audit(), [])

    def test_authorized_motor_output_requires_run_bound_measured_audit(self) -> None:
        checker = CHECKER.GazeboFlightCheck.__new__(CHECKER.GazeboFlightCheck)
        checker.callsign = "drone-1"
        checker.run_id = "run-1"
        valid = {
            "contract": CHECKER.ACTUATOR_OUTPUT_CONTRACT,
            "version": CHECKER.ACTUATOR_OUTPUT_VERSION,
            "runId": "run-1",
            "vehicleId": "drone-1",
            "actuatorKind": "motors",
            "sequence": 12,
            "stage": "authorized-command",
            "neutral": False,
            "authority": "verified-core-grant",
            "worldSha256": "sha256:" + "a" * 64,
            "frameId": "enu-test",
            "values": {"velocity": [100.0, 100.0, 100.0, 100.0]},
            "executionGrant": {
                "contract": "furia.prime.execution-grant",
                "vehicleId": "drone-1",
                "runId": "run-1",
                "worldDigest": "sha256:" + "a" * 64,
                "worldFrameId": "enu-test",
                "targetEnuMm": [1000, 2000, 3000],
            },
        }
        checker.actuator_audit = mock.Mock(return_value=[valid])
        matched = asyncio.run(
            checker.wait_authorized_motor_output(10, (1.0, 2.0, 3.0), timeout_s=0.01)
        )
        self.assertIs(matched, valid)

        invalid_rows = (
            {**valid, "runId": "another-run"},
            {**valid, "authority": "safe-stop"},
            {**valid, "values": {"velocity": [0, 0, 0, 0]}},
            {**valid, "worldSha256": "sha256:bad"},
            {**valid, "sequence": True},
            {
                **valid,
                "executionGrant": {**valid["executionGrant"], "worldDigest": "sha256:" + "b" * 64},
            },
            {
                **valid,
                "executionGrant": {**valid["executionGrant"], "worldFrameId": "other-frame"},
            },
            {
                **valid,
                "executionGrant": {**valid["executionGrant"], "targetEnuMm": [1000, True, 3000]},
            },
            {**valid, "executionGrant": {**valid["executionGrant"], "vehicleId": "drone-2"}},
        )
        for invalid in invalid_rows:
            checker.actuator_audit = mock.Mock(return_value=[invalid])
            with self.subTest(invalid=invalid):
                with self.assertRaisesRegex(
                    AssertionError, "no independent authorized motor output"
                ):
                    asyncio.run(
                        checker.wait_authorized_motor_output(10, (1.0, 2.0, 3.0), timeout_s=0.001)
                    )

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
