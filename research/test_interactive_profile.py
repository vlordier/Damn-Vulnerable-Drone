from __future__ import annotations

import base64
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import interactive_profile


class InteractiveProfileTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.real_port_status = interactive_profile._run_owned_port_statuses
        self.port_status = patch(
            "interactive_profile._run_owned_port_statuses",
            return_value={
                "natsPort": "closed",
                "bridgeUdpBindPort": "closed",
                "qgcUdpListenPort": "closed",
            },
        )
        self.port_status_mock = self.port_status.start()
        self.addCleanup(self.port_status.stop)
        self.heartbeat = patch(
            "interactive_profile.sample_qgc_heartbeat",
            return_value={"status": "unverified", "reason": "no latest audit frame"},
        )
        self.heartbeat.start()
        self.addCleanup(self.heartbeat.stop)
        self.bridge_heartbeat = patch(
            "interactive_profile.sample_bridge_heartbeat",
            return_value={"status": "unverified", "reason": "no latest bridge audit frame"},
        )
        self.bridge_heartbeat.start()
        self.addCleanup(self.bridge_heartbeat.stop)
        self.receipt = Path(self.temp.name) / "interactive-profile-receipt.json"
        bridge_identity = Path(self.temp.name) / ".bridge.identity"
        bridge_control = bridge_identity.with_name(
            bridge_identity.name + ".control.0123456789abcdef0123456789abcdef"
        )
        bridge_identity.write_text(
            f"pid=8991\nchild_pid=9001\ngroup_owned=1\nfifo={bridge_control}\n",
            encoding="utf-8",
        )
        bridge_identity.chmod(0o600)
        self.receipt.write_text(
            json.dumps(
                {
                    "contract": interactive_profile.PROFILE_CONTRACT,
                    "version": interactive_profile.PROFILE_VERSION,
                    "profile": "interactive-antoine-gazebo",
                    "runId": Path(self.temp.name).name,
                    "runDirectory": self.temp.name,
                    "callsign": "drone-1",
                    "ports": {"nats": 14226, "qgcUdpListen": 14550, "bridgeUdpBind": 14651},
                    "status": "ready",
                    "processes": {
                        "mavlinkBridge": {
                            "pid": 9001,
                            "identity": "owned-process --qgc-peer 127.0.0.1:14550 --qgc-bind 127.0.0.1:14651",
                            "supervisorIdentityPath": str(bridge_identity),
                        },
                        "qgcUserProcess": {"pid": 9002, "identity": "qgc-process"},
                    },
                }
            ),
            encoding="utf-8",
        )
        flight_directory = Path(self.temp.name) / "flight-proof"
        (flight_directory / "logs").mkdir(parents=True, exist_ok=True)
        (flight_directory / "logs" / "nats.log").write_text(
            "Listening for client connections on 127.0.0.1:14226\nServer is ready\n",
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def add_supervisor_process(self, receipt: dict) -> None:
        identity_path = Path(self.temp.name) / ".antoine.identity"
        control_path = identity_path.with_name(
            identity_path.name + ".control.0123456789abcdef0123456789abcdef"
        )
        identity_path.write_text(
            f"pid=8993\nchild_pid=9003\ngroup_owned=1\nfifo={control_path}\n",
            encoding="utf-8",
        )
        identity_path.chmod(0o600)
        receipt["processes"]["antoineGazeboSupervisor"] = {
            "pid": 9003,
            "identity": "supervisor-process",
            "supervisorIdentityPath": str(identity_path),
        }

    def test_status_rejects_non_object_receipt(self) -> None:
        self.receipt.write_text("[]", encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "receipt must be a JSON object"):
            interactive_profile.inspect(self.receipt)

    def test_audited_heartbeat_decodes_armed_state_from_mavlink_v2(self) -> None:
        payload = bytes((0, 0, 0, 0, 2, 3, 0x80, 4, 3))
        frame = bytes((0xFD, len(payload), 0, 0, 1, 1, 0, 0, 0, 0)) + payload + b"\x00\x00"
        state = interactive_profile._decode_heartbeat_state(base64.b64encode(frame).decode())

        self.assertEqual(state["armed"], True)
        self.assertEqual(state["mode"], "STABILIZE")
        self.assertEqual(state["customMode"], 0)
        self.assertIsNone(interactive_profile._decode_heartbeat_state("not-base64"))

    def test_live_heartbeat_requires_valid_mavlink_crc(self) -> None:
        payload = bytes((0, 0, 0, 0, 2, 3, 0, 4, 3))
        header = bytes((0xFD, len(payload), 0, 0, 0, 1, 1, 0, 0, 0))
        crc = interactive_profile._mavlink_x25(header[1:] + payload + bytes((50,)))
        frame = header + payload + crc.to_bytes(2, "little")

        self.assertEqual(interactive_profile._decode_live_heartbeat(frame)["armed"], False)
        self.assertIsNone(interactive_profile._decode_live_heartbeat(frame[:-1] + b"\x00"))

    def test_headless_profile_reports_fresh_disarmed_state_without_qgc(self) -> None:
        receipt = json.loads(self.receipt.read_text(encoding="utf-8"))
        receipt["profile"] = interactive_profile.HEADLESS_PROFILE
        receipt["processes"].pop("qgcUserProcess")
        self.add_supervisor_process(receipt)
        self.receipt.write_text(json.dumps(receipt), encoding="utf-8")
        with (
            patch(
                "interactive_profile.process_identity",
                side_effect={
                    9001: receipt["processes"]["mavlinkBridge"]["identity"],
                    9003: "supervisor-process",
                }.get,
            ),
            patch(
                "interactive_profile._run_owned_port_statuses",
                return_value={"natsPort": "open", "bridgeUdpBindPort": "open"},
            ),
            patch(
                "interactive_profile.subprocess.run",
                return_value=type("Result", (), {"returncode": 0, "stdout": "", "stderr": ""})(),
            ),
            patch(
                "interactive_profile.sample_telemetry",
                return_value={
                    "status": "fresh",
                    "armed": None,
                    "mode": None,
                    "simulationTimeSeconds": 5.0,
                },
            ),
            patch(
                "interactive_profile.sample_bridge_heartbeat",
                return_value={"status": "unverified"},
            ),
            patch(
                "interactive_profile.sample_headless_bridge_heartbeat",
                return_value={
                    "status": "observed",
                    "armed": False,
                    "mode": "STABILIZE",
                    "stateSource": "CRC-validated live MAVLink HEARTBEAT from configured bridge UDP peer",
                },
            ),
            patch(
                "interactive_profile.sample_gazebo_pose_recorder",
                return_value={"status": "fresh", "simulationTimeSeconds": 5.2},
            ),
        ):
            result = interactive_profile.inspect(self.receipt)

        self.assertEqual(result["status"], "recorded", result)
        self.assertEqual(result["armed"], False)
        self.assertEqual(
            result["telemetry"]["stateSource"], result["bridgeHeartbeat"]["stateSource"]
        )
        self.assertEqual(result["qgcHeartbeat"]["status"], "not-applicable")
        self.assertEqual(result["qgcPeerRouting"]["status"], "not-applicable")
        self.assertEqual(result["gazeboClockAlignment"]["status"], "aligned")

    def test_gazebo_clock_alignment_rejects_misaligned_and_invalid_samples(self) -> None:
        telemetry = {"status": "fresh", "simulationTimeSeconds": 10.0}
        pose = {"status": "fresh", "simulationTimeSeconds": 10.8}
        self.assertEqual(
            interactive_profile.align_gazebo_simulation_time(telemetry, pose)["status"],
            "aligned",
        )
        pose["simulationTimeSeconds"] = 11.01
        misaligned = interactive_profile.align_gazebo_simulation_time(telemetry, pose)
        self.assertEqual(misaligned["status"], "misaligned")
        self.assertAlmostEqual(misaligned["absoluteSkewSeconds"], 1.01)
        for value in (True, -1.0, float("nan"), float("inf"), "10.0"):
            pose["simulationTimeSeconds"] = value
            self.assertEqual(
                interactive_profile.align_gazebo_simulation_time(telemetry, pose)["status"],
                "invalid",
            )

    def test_status_stays_partial_when_nats_and_gazebo_clocks_do_not_align(self) -> None:
        receipt = json.loads(self.receipt.read_text(encoding="utf-8"))
        receipt["profile"] = interactive_profile.HEADLESS_PROFILE
        receipt["processes"].pop("qgcUserProcess")
        self.add_supervisor_process(receipt)
        self.receipt.write_text(json.dumps(receipt), encoding="utf-8")
        with (
            patch(
                "interactive_profile.process_identity",
                side_effect={
                    9001: receipt["processes"]["mavlinkBridge"]["identity"],
                    9003: "supervisor-process",
                }.get,
            ),
            patch(
                "interactive_profile._run_owned_port_statuses",
                return_value={"natsPort": "open", "bridgeUdpBindPort": "open"},
            ),
            patch(
                "interactive_profile.subprocess.run",
                return_value=type("Result", (), {"returncode": 0, "stdout": "", "stderr": ""})(),
            ),
            patch(
                "interactive_profile.sample_telemetry",
                return_value={
                    "status": "fresh",
                    "armed": False,
                    "mode": "STABILIZE",
                    "simulationTimeSeconds": 5.0,
                },
            ),
            patch(
                "interactive_profile.sample_bridge_heartbeat",
                return_value={"status": "unverified"},
            ),
            patch(
                "interactive_profile.sample_headless_bridge_heartbeat",
                return_value={"status": "observed", "armed": False, "mode": "STABILIZE"},
            ),
            patch(
                "interactive_profile.sample_gazebo_pose_recorder",
                return_value={"status": "fresh", "simulationTimeSeconds": 8.0},
            ),
        ):
            result = interactive_profile.inspect(self.receipt)

        self.assertEqual(result["gazeboClockAlignment"]["status"], "misaligned")
        self.assertEqual(result["status"], "partial")

    def test_status_rejects_receipt_outside_run_directory_before_probes(self) -> None:
        other_run = Path(self.temp.name) / "other-run"
        other_run.mkdir()
        receipt = json.loads(self.receipt.read_text(encoding="utf-8"))
        receipt["runId"] = "other-run"
        receipt["runDirectory"] = str(other_run)
        self.receipt.write_text(json.dumps(receipt), encoding="utf-8")
        with (
            patch("interactive_profile.process_identity") as process_identity,
            patch("interactive_profile.subprocess.run") as run,
        ):
            result = interactive_profile.inspect(self.receipt)

        process_identity.assert_not_called()
        run.assert_not_called()
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["runIdentityValidation"]["status"], "invalid")
        self.assertEqual(result["processValidation"]["status"], "not-checked")
        self.assertEqual(result["bridgeHeartbeat"]["status"], "unverified")
        self.assertEqual(result["gazeboPoseRecorder"]["status"], "unverified")

    def test_status_rejects_invalid_callsign_before_probes(self) -> None:
        receipt = json.loads(self.receipt.read_text(encoding="utf-8"))
        receipt["callsign"] = "drone.one"
        self.receipt.write_text(json.dumps(receipt), encoding="utf-8")
        with (
            patch("interactive_profile.process_identity") as process_identity,
            patch("interactive_profile.subprocess.run") as run,
        ):
            result = interactive_profile.inspect(self.receipt)

        process_identity.assert_not_called()
        run.assert_not_called()
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["telemetry"]["status"], "invalid")
        self.assertIn("single NATS subject token", result["telemetry"]["reason"])
        self.assertEqual(result["bridgeHeartbeat"]["status"], "unverified")
        self.assertEqual(result["qgcPeerRouting"]["status"], "unverified")
        self.assertEqual(result["gazeboPoseRecorder"]["status"], "unverified")

    def test_sample_telemetry_rejects_native_antoine_protocol_for_gazebo_profile(self) -> None:
        native_samples = [
            {
                "schema": "furia.telemetry",
                "version": "1.0.0",
                "protocol": "antoine",
                "identity": "drone-1",
                "time_s": simulation_time,
                "armed": False,
            }
            for simulation_time in (1.0, 1.1)
        ]
        with patch(
            "interactive_profile.read_nats_messages", return_value=(native_samples, None)
        ) as read_messages:
            result = interactive_profile.sample_telemetry(
                json.loads(self.receipt.read_text(encoding="utf-8"))
            )

        self.assertEqual(result["status"], "invalid")
        self.assertIn("Gazebo protocol", result["reason"])
        read_messages.assert_called_once_with(14226, "furia.telemetry.drone-1")

    def test_stop_rejects_unsupported_receipt_before_process_inspection(self) -> None:
        receipt = json.loads(self.receipt.read_text(encoding="utf-8"))
        receipt["version"] = "9.9.9"
        self.receipt.write_text(json.dumps(receipt), encoding="utf-8")
        with patch("interactive_profile.process_identity") as process_identity:
            with self.assertRaisesRegex(ValueError, "unsupported interactive profile receipt"):
                interactive_profile.stop_owned(self.receipt)

        process_identity.assert_not_called()

    @patch("interactive_profile.process_identity", return_value="foreign-process")
    def test_status_marks_pid_reuse_as_partial_without_attributing_process(self, _identity) -> None:
        with patch("interactive_profile.subprocess.run") as run:
            run.return_value.returncode = 0
            result = interactive_profile.inspect(self.receipt)
        self.assertEqual(result["status"], "partial")
        self.assertFalse(result["processes"]["mavlinkBridge"]["identityMatches"])
        self.assertIsNone(result["armed"])

    def test_status_reports_identity_probe_os_error_as_partial(self) -> None:
        with (
            patch("interactive_profile.process_identity", side_effect=OSError("ps unavailable")),
            patch("interactive_profile.subprocess.run") as run,
            patch(
                "interactive_profile.read_nats_messages",
                return_value=([], {"status": "unavailable", "reason": "no telemetry"}),
            ),
        ):
            run.return_value.returncode = 1
            result = interactive_profile.inspect(self.receipt)

        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["processes"]["mavlinkBridge"]["alive"], None)
        self.assertEqual(
            result["processes"]["mavlinkBridge"]["error"], "identity-probe-unavailable"
        )

    def test_process_identity_treats_ps_error_as_unavailable_not_absent(self) -> None:
        with patch(
            "interactive_profile.subprocess.run",
            return_value=type(
                "Result", (), {"returncode": 2, "stdout": "", "stderr": "ps error"}
            )(),
        ):
            with self.assertRaisesRegex(OSError, "ps error"):
                interactive_profile.process_identity(9001)

    def test_process_identity_bounds_ps_probe(self) -> None:
        with patch(
            "interactive_profile.subprocess.run",
            side_effect=subprocess.TimeoutExpired(
                "ps", interactive_profile.PROCESS_IDENTITY_TIMEOUT_SECONDS
            ),
        ) as run:
            with self.assertRaisesRegex(OSError, "timed out"):
                interactive_profile.process_identity(9001)

        self.assertEqual(
            run.call_args.kwargs["timeout"], interactive_profile.PROCESS_IDENTITY_TIMEOUT_SECONDS
        )

    def test_status_rejects_boolean_port_without_probing_it(self) -> None:
        receipt = json.loads(self.receipt.read_text(encoding="utf-8"))
        receipt["ports"]["nats"] = True
        self.receipt.write_text(json.dumps(receipt), encoding="utf-8")
        with (
            patch("interactive_profile.process_identity", return_value=None),
            patch("interactive_profile.subprocess.run") as run,
        ):
            run.return_value.returncode = 1
            result = interactive_profile.inspect(self.receipt)

        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["portValidation"]["status"], "invalid")
        self.assertIn("nats must be an integer", result["portValidation"]["errors"][0])
        self.assertIsNone(result["processes"]["natsPort"]["listening"])
        self.assertNotIn("-iTCP:True", str([call.args[0] for call in run.call_args_list]))

    def test_status_marks_lsof_errors_unavailable_and_partial(self) -> None:
        with (
            patch("interactive_profile.process_identity", return_value=None),
            patch("interactive_profile.read_nats_messages") as read_messages,
            patch("interactive_profile.subprocess.run") as run,
        ):
            read_messages.return_value = (
                [],
                {"status": "unavailable", "reason": "no fresh telemetry"},
            )
            run.side_effect = [
                type("Result", (), {"returncode": 2})(),
                type("Result", (), {"returncode": 0})(),
                type("Result", (), {"returncode": 0})(),
            ]
            result = interactive_profile.inspect(self.receipt)

        self.assertEqual(result["status"], "partial")
        self.assertIsNone(result["processes"]["natsPort"]["listening"])
        self.assertEqual(result["processes"]["natsPort"]["error"], "port-probe-failed")

    def test_status_bounds_lsof_probes_and_reports_timeout_unavailable(self) -> None:
        with (
            patch("interactive_profile.process_identity", return_value=None),
            patch(
                "interactive_profile.read_nats_messages",
                return_value=([], {"status": "unavailable", "reason": "no fresh telemetry"}),
            ),
            patch("interactive_profile.subprocess.run") as run,
        ):
            run.side_effect = [
                subprocess.TimeoutExpired("lsof", interactive_profile.PORT_PROBE_TIMEOUT_SECONDS),
                type("Result", (), {"returncode": 0})(),
                type("Result", (), {"returncode": 0})(),
            ]
            result = interactive_profile.inspect(self.receipt)

        self.assertEqual(result["status"], "partial")
        self.assertIsNone(result["processes"]["natsPort"]["listening"])
        self.assertEqual(result["processes"]["natsPort"]["error"], "port-probe-unavailable")
        self.assertEqual(
            run.call_args_list[0].kwargs["timeout"], interactive_profile.PORT_PROBE_TIMEOUT_SECONDS
        )

    def test_stop_port_probes_are_bounded(self) -> None:
        self.port_status.stop()
        with patch("interactive_profile.subprocess.run") as run:
            run.side_effect = subprocess.TimeoutExpired(
                "lsof", interactive_profile.PORT_PROBE_TIMEOUT_SECONDS
            )
            result = interactive_profile._run_owned_port_statuses(
                json.loads(self.receipt.read_text(encoding="utf-8"))
            )

        self.assertEqual(
            result,
            {
                "natsPort": "unavailable",
                "bridgeUdpBindPort": "unavailable",
                "qgcUdpListenPort": "unavailable",
            },
        )
        self.assertTrue(
            all(
                call.kwargs["timeout"] == interactive_profile.PORT_PROBE_TIMEOUT_SECONDS
                for call in run.call_args_list
            )
        )

    def test_profile_ports_reject_duplicate_udp_bindings(self) -> None:
        valid, errors = interactive_profile._validate_profile_ports(
            {"nats": 14226, "qgcUdpListen": 14651, "bridgeUdpBind": 14651}
        )

        self.assertFalse(valid)
        self.assertEqual(errors, ["qgcUdpListen and bridgeUdpBind must use different UDP ports"])

    def test_status_reports_malformed_process_records_as_partial(self) -> None:
        receipt = json.loads(self.receipt.read_text(encoding="utf-8"))
        receipt["processes"] = {"mavlinkBridge": None}
        self.receipt.write_text(json.dumps(receipt), encoding="utf-8")
        with (
            patch("interactive_profile.process_identity") as process_identity,
            patch("interactive_profile.subprocess.run") as run,
            patch("interactive_profile.read_nats_messages") as read_messages,
        ):
            run.return_value.returncode = 0
            read_messages.return_value = (
                [],
                {"status": "unavailable", "reason": "no fresh telemetry"},
            )
            result = interactive_profile.inspect(self.receipt)

        process_identity.assert_not_called()
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["processValidation"]["status"], "invalid")
        self.assertEqual(result["processes"]["mavlinkBridge"]["error"], "invalid-process-record")

    def test_status_requires_supervisor_identity_even_with_fresh_telemetry(self) -> None:
        telemetry = {
            "schema": "furia.telemetry",
            "version": "1.0.0",
            "protocol": "antoine-gz",
            "identity": "drone-1",
            "time_s": 4.0,
            "armed": False,
        }
        with (
            patch(
                "interactive_profile.process_identity",
                return_value="owned-process --qgc-peer 127.0.0.1:14550 --qgc-bind 127.0.0.1:14651",
            ),
            patch("interactive_profile.subprocess.run") as run,
            patch(
                "interactive_profile.read_nats_messages",
                return_value=([{**telemetry, "time_s": 3.9}, telemetry], None),
            ),
        ):
            run.return_value.returncode = 0
            result = interactive_profile.inspect(self.receipt)

        self.assertEqual(result["telemetry"]["status"], "fresh")
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["processValidation"]["status"], "incomplete")

    @patch(
        "interactive_profile.process_identity",
        side_effect=[
            "owned-process --qgc-peer 127.0.0.1:14550 --qgc-bind 127.0.0.1:14651",
            "qgc-process",
            "supervisor-process",
        ],
    )
    def test_status_reports_fresh_callsign_bound_armed_state(self, _identity) -> None:
        run_directory = Path(self.temp.name)
        flight_directory = run_directory / "flight-proof"
        (flight_directory / "logs").mkdir(parents=True, exist_ok=True)
        (flight_directory / "logs" / "nats.log").write_text(
            "Listening for client connections on 127.0.0.1:14226\nServer is ready\n",
            encoding="utf-8",
        )
        (flight_directory / "gazebo-pose.jsonl").write_text(
            "\n".join(
                json.dumps(
                    {
                        "header": {"stamp": {"sec": 12, "nsec": nsec}},
                        "pose": [{"name": "isr", "position": {"x": 0.1, "y": 0.2, "z": 0.0}}],
                    }
                )
                for nsec in (400_000_000, 500_000_000)
            )
            + "\n",
            encoding="utf-8",
        )
        receipt = json.loads(self.receipt.read_text(encoding="utf-8"))
        self.add_supervisor_process(receipt)
        receipt["flightRunDirectory"] = str(flight_directory)
        self.receipt.write_text(json.dumps(receipt), encoding="utf-8")
        telemetry = {
            "schema": "furia.telemetry",
            "version": "1.0.0",
            "protocol": "antoine-gz",
            "identity": "drone-1",
            "time_s": 12.5,
            "armed": False,
            "mode": "SAFE-HOLD",
        }
        with patch("interactive_profile.subprocess.run") as run:

            def result_for(command, **_kwargs):
                result = type("Result", (), {"returncode": 0, "stdout": ""})()
                if command[0] == "nats":
                    first = {**telemetry, "time_s": 12.4}
                    result.stdout = f"{json.dumps(first)}\n{json.dumps(telemetry)}\n"
                return result

            run.side_effect = result_for
            result = interactive_profile.inspect(self.receipt)
            telemetry.pop("armed")
            _identity.side_effect = [
                "owned-process --qgc-peer 127.0.0.1:14550 --qgc-bind 127.0.0.1:14651",
                "qgc-process",
                "supervisor-process",
            ]
            unknown_armed = interactive_profile.inspect(self.receipt)
        self.assertEqual(result["telemetry"]["status"], "fresh")
        self.assertEqual(result["telemetry"]["protocol"], "antoine-gz")
        self.assertFalse(result["armed"])
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["bridgeHeartbeat"]["status"], "unverified")
        self.assertEqual(
            result["bridgeHeartbeat"]["reason"],
            "no latest bridge audit frame",
        )
        self.assertEqual(result["qgcHeartbeat"]["status"], "unverified")
        self.assertEqual(result["qgcHeartbeat"]["reason"], "no latest audit frame")
        self.assertEqual(result["qgcPeerRouting"]["status"], "configured-unverified")
        self.assertEqual(result["gazeboPoseRecorder"]["status"], "fresh")
        self.assertEqual(result["gazeboPoseRecorder"]["poseName"], "isr")
        self.assertIn("no corroborated fresh heartbeat", result["qgcPeerRouting"]["reason"])
        self.assertAlmostEqual(result["gazeboPoseRecorder"]["simulationTimeAdvanceSeconds"], 0.1)
        self.assertEqual(unknown_armed["telemetry"]["status"], "fresh")
        self.assertIsNone(unknown_armed["armed"])
        self.assertEqual(unknown_armed["status"], "partial")
        self.assertIn("did not provide", unknown_armed["armedReason"])

    def test_audit_heartbeat_is_read_with_last_for_and_bound_to_run_callsign(self) -> None:
        self.heartbeat.stop()
        envelope = {
            "schema": "furia.mavlink.frame",
            "version": "1.0.0",
            "callsign": "drone-1",
            "direction": "gcs_to_antoine",
            "received_at_unix_ms": int(interactive_profile.time.time() * 1000),
            "source": "127.0.0.1:53000",
            "message_id": 0,
        }
        output = json.dumps({"data": base64.b64encode(json.dumps(envelope).encode()).decode()})
        with patch("interactive_profile.subprocess.run") as run:
            run.return_value.returncode = 0
            run.return_value.stdout = output
            result = interactive_profile.sample_qgc_heartbeat(
                json.loads(self.receipt.read_text(encoding="utf-8"))
            )

        self.assertEqual(result["status"], "observed")
        command = run.call_args.args[0]
        self.assertEqual(
            command[-5:],
            [
                "stream",
                "get",
                "ANTOINE_MAVLINK",
                "--last-for=furia.mavlink.command.drone-1",
                "--json",
            ],
        )
        self.assertIn("stream", command)
        self.assertNotIn("sub", command)
        self.assertNotIn("publish", command)

    def test_audit_heartbeat_rejects_stale_and_non_heartbeat_frames(self) -> None:
        self.heartbeat.stop()
        receipt = json.loads(self.receipt.read_text(encoding="utf-8"))
        envelope = {
            "schema": "furia.mavlink.frame",
            "version": "1.0.0",
            "callsign": "drone-1",
            "direction": "gcs_to_antoine",
            "received_at_unix_ms": 1,
            "source": "127.0.0.1:53000",
            "message_id": 0,
        }
        with patch("interactive_profile.subprocess.run") as run:
            run.return_value.returncode = 0
            run.return_value.stdout = json.dumps({"data": json.dumps(envelope)})
            stale = interactive_profile.sample_qgc_heartbeat(receipt)
            envelope["received_at_unix_ms"] = int(interactive_profile.time.time() * 1000)
            envelope["message_id"] = 76
            run.return_value.stdout = json.dumps({"data": json.dumps(envelope)})
            other_frame = interactive_profile.sample_qgc_heartbeat(receipt)

        self.assertEqual(stale["status"], "stale")
        self.assertEqual(other_frame["status"], "unverified")

    def test_bridge_heartbeat_reads_dedicated_vehicle_to_gcs_audit(self) -> None:
        self.heartbeat.stop()
        self.bridge_heartbeat.stop()
        envelope = {
            "schema": "furia.mavlink.frame",
            "version": "1.0.0",
            "callsign": "drone-1",
            "direction": "vehicle_to_gcs",
            "received_at_unix_ms": int(interactive_profile.time.time() * 1000),
            "source": "127.0.0.1:14651",
            "message_id": 0,
        }
        output = json.dumps({"data": base64.b64encode(json.dumps(envelope).encode()).decode()})
        with patch("interactive_profile.subprocess.run") as run:
            run.return_value.returncode = 0
            run.return_value.stdout = output
            result = interactive_profile.sample_bridge_heartbeat(
                json.loads(self.receipt.read_text(encoding="utf-8"))
            )

        self.assertEqual(result["status"], "observed")
        command = run.call_args.args[0]
        self.assertIn("--last-for=furia.mavlink.heartbeat.drone-1", command)

    def test_bridge_heartbeat_rejects_inbound_direction(self) -> None:
        self.heartbeat.stop()
        self.bridge_heartbeat.stop()
        envelope = {
            "schema": "furia.mavlink.frame",
            "version": "1.0.0",
            "callsign": "drone-1",
            "direction": "gcs_to_antoine",
            "received_at_unix_ms": int(interactive_profile.time.time() * 1000),
            "source": "127.0.0.1:53000",
            "message_id": 0,
        }
        with patch("interactive_profile.subprocess.run") as run:
            run.return_value.returncode = 0
            run.return_value.stdout = json.dumps({"data": json.dumps(envelope)})
            result = interactive_profile.sample_bridge_heartbeat(
                json.loads(self.receipt.read_text(encoding="utf-8"))
            )

        self.assertEqual(result["status"], "unverified")

    def test_status_observes_configured_path_only_with_live_recorded_qgc(self) -> None:
        receipt = json.loads(self.receipt.read_text(encoding="utf-8"))
        self.add_supervisor_process(receipt)
        self.receipt.write_text(json.dumps(receipt), encoding="utf-8")
        with (
            patch(
                "interactive_profile.process_identity",
                side_effect={
                    9001: receipt["processes"]["mavlinkBridge"]["identity"],
                    9002: "qgc-process",
                    9003: "supervisor-process",
                }.get,
            ),
            patch("interactive_profile.sample_qgc_heartbeat", return_value={"status": "observed"}),
            patch(
                "interactive_profile.subprocess.run",
                return_value=type("Result", (), {"returncode": 1})(),
            ),
        ):
            result = interactive_profile.inspect(self.receipt)

        self.assertEqual(result["qgcPeerRouting"]["status"], "observed")
        self.assertIn("not authenticated", result["qgcPeerRouting"]["reason"])

    def test_status_does_not_attribute_heartbeat_to_qgc_without_live_process(self) -> None:
        with (
            patch("interactive_profile.process_identity", return_value=None),
            patch("interactive_profile.sample_qgc_heartbeat", return_value={"status": "observed"}),
            patch(
                "interactive_profile.subprocess.run",
                return_value=type("Result", (), {"returncode": 1})(),
            ),
        ):
            result = interactive_profile.inspect(self.receipt)

        self.assertEqual(result["qgcHeartbeat"]["status"], "observed")
        self.assertEqual(result["bridgeHeartbeat"]["status"], "unverified")
        self.assertEqual(result["qgcPeerRouting"]["status"], "unverified")

    def test_pose_recorder_rejects_nonadvancing_simulation_time(self) -> None:
        flight_directory = Path(self.temp.name) / "flight-proof"
        flight_directory.mkdir(exist_ok=True)
        pose_path = flight_directory / "gazebo-pose.jsonl"
        sample = {
            "header": {"stamp": {"sec": 5, "nsec": 0}},
            "pose": [{"name": "isr", "position": {"x": 0.0, "y": 0.0, "z": 0.0}}],
        }
        pose_path.write_text(f"{json.dumps(sample)}\n{json.dumps(sample)}\n", encoding="utf-8")
        receipt = json.loads(self.receipt.read_text(encoding="utf-8"))
        receipt["flightRunDirectory"] = str(flight_directory)

        result = interactive_profile.sample_gazebo_pose_recorder(receipt)

        self.assertEqual(result["status"], "stale")
        self.assertIn("two advancing", result["reason"])

    def test_pose_recorder_rejects_alternate_in_run_evidence_directory(self) -> None:
        flight_directory = Path(self.temp.name) / "unrelated-evidence"
        flight_directory.mkdir()
        receipt = json.loads(self.receipt.read_text(encoding="utf-8"))
        receipt["flightRunDirectory"] = str(flight_directory)

        result = interactive_profile.sample_gazebo_pose_recorder(receipt)

        self.assertEqual(result["status"], "invalid")
        self.assertIn("does not match the run-owned path", result["reason"])

    def test_pose_recorder_rejects_symlink_alias_for_flight_directory(self) -> None:
        flight_directory = Path(self.temp.name) / "flight-proof"
        flight_directory.mkdir(exist_ok=True)
        alias = Path(self.temp.name) / "flight-proof-alias"
        alias.symlink_to(flight_directory, target_is_directory=True)
        receipt = json.loads(self.receipt.read_text(encoding="utf-8"))
        receipt["flightRunDirectory"] = str(alias)

        result = interactive_profile.sample_gazebo_pose_recorder(receipt)

        self.assertEqual(result["status"], "invalid")
        self.assertIn("does not match the run-owned path", result["reason"])

    def test_pose_recorder_accepts_advancing_protobuf_json_timestamp_strings(self) -> None:
        flight_directory = Path(self.temp.name) / "flight-proof"
        flight_directory.mkdir(exist_ok=True)
        pose_path = flight_directory / "gazebo-pose.jsonl"
        samples = [
            {
                "header": {"stamp": {"sec": str(second), "nsec": "250000000"}},
                "pose": [{"name": "isr", "position": {"x": 0.1, "y": 0.2, "z": 0.3}}],
            }
            for second in (5, 6)
        ]
        pose_path.write_text(
            "".join(f"{json.dumps(sample)}\n" for sample in samples), encoding="utf-8"
        )
        receipt = json.loads(self.receipt.read_text(encoding="utf-8"))
        receipt["flightRunDirectory"] = str(flight_directory)

        result = interactive_profile.sample_gazebo_pose_recorder(receipt)

        self.assertEqual(result["status"], "fresh")
        self.assertEqual(result["poseName"], "isr")
        self.assertEqual(result["simulationTimeAdvanceSeconds"], 1.0)

    def test_pose_recorder_rejects_missing_position_coordinate(self) -> None:
        flight_directory = Path(self.temp.name) / "flight-proof"
        flight_directory.mkdir(exist_ok=True)
        pose_path = flight_directory / "gazebo-pose.jsonl"
        samples = [
            {
                "header": {"stamp": {"sec": second, "nsec": 0}},
                "pose": [{"name": "isr", "position": {"x": 0.0, "y": 0.0}}],
            }
            for second in (5, 6)
        ]
        pose_path.write_text(
            "".join(f"{json.dumps(sample)}\n" for sample in samples), encoding="utf-8"
        )
        receipt = json.loads(self.receipt.read_text(encoding="utf-8"))
        receipt["flightRunDirectory"] = str(flight_directory)

        result = interactive_profile.sample_gazebo_pose_recorder(receipt)

        self.assertEqual(result["status"], "stale")
        self.assertIn("two advancing finite vehicle pose samples", result["reason"])

    def test_pose_recorder_selects_vehicle_pose_when_other_models_precede_it(self) -> None:
        flight_directory = Path(self.temp.name) / "flight-proof"
        flight_directory.mkdir(exist_ok=True)
        pose_path = flight_directory / "gazebo-pose.jsonl"
        samples = []
        for second in (5, 6):
            samples.append(
                {
                    "header": {"stamp": {"sec": second, "nsec": 0}},
                    "pose": [
                        {"name": "ground_plane", "position": {"x": 0, "y": 0, "z": 0}},
                        {"name": "target_alpha", "position": {"x": 23.95, "y": 16, "z": 0.1}},
                        {"name": "isr", "position": {"x": 1, "y": 2, "z": 8}},
                    ],
                }
            )
        pose_path.write_text(
            "".join(f"{json.dumps(sample)}\n" for sample in samples), encoding="utf-8"
        )
        receipt = json.loads(self.receipt.read_text(encoding="utf-8"))
        receipt["flightRunDirectory"] = str(flight_directory)

        result = interactive_profile.sample_gazebo_pose_recorder(receipt)

        self.assertEqual(result["status"], "fresh")
        self.assertEqual(result["poseName"], "isr")

    def test_pose_recorder_rejects_advancing_non_vehicle_models(self) -> None:
        flight_directory = Path(self.temp.name) / "flight-proof"
        flight_directory.mkdir(exist_ok=True)
        pose_path = flight_directory / "gazebo-pose.jsonl"
        samples = [
            {
                "header": {"stamp": {"sec": second, "nsec": 0}},
                "pose": [
                    {"name": "ground_plane", "position": {"x": 0, "y": 0, "z": 0}},
                    {"name": "target_alpha", "position": {"x": 23.95, "y": 16, "z": 0.1}},
                ],
            }
            for second in (5, 6)
        ]
        pose_path.write_text(
            "".join(f"{json.dumps(sample)}\n" for sample in samples), encoding="utf-8"
        )
        receipt = json.loads(self.receipt.read_text(encoding="utf-8"))
        receipt["flightRunDirectory"] = str(flight_directory)

        result = interactive_profile.sample_gazebo_pose_recorder(receipt)

        self.assertEqual(result["status"], "stale")
        self.assertIn("vehicle pose samples", result["reason"])

    @patch(
        "interactive_profile.process_identity",
        side_effect=[
            "owned-process --qgc-peer 127.0.0.1:14550 --qgc-bind 127.0.0.1:14651",
            "qgc-process",
        ],
    )
    def test_status_uses_advancing_gazebo_time_when_telemetry_omits_sim_time(
        self, _identity
    ) -> None:
        telemetry = {
            "schema": "furia.telemetry",
            "version": "1.0.0",
            "protocol": "antoine-gz",
            "identity": "drone-1",
            "armed": False,
        }
        states = [
            {
                "schema": "furia.gzsitl.state",
                "version": "1.1.0",
                "identity": "drone-1",
                "simulation_time_ns": 1_000_000_000,
            },
            {
                "schema": "furia.gzsitl.state",
                "version": "1.1.0",
                "identity": "drone-1",
                "simulation_time_ns": 1_100_000_000,
            },
        ]
        with patch("interactive_profile.subprocess.run") as run:

            def result_for(command, **_kwargs):
                result = type("Result", (), {"returncode": 0, "stdout": ""})()
                if command[0] == "nats":
                    samples = (
                        states if command[-1] == "furia.gzsitl.state.drone-1" else [telemetry] * 2
                    )
                    result.stdout = "\n".join(json.dumps(sample) for sample in samples) + "\n"
                return result

            run.side_effect = result_for
            result = interactive_profile.inspect(self.receipt)
        self.assertEqual(result["telemetry"]["status"], "fresh")
        self.assertEqual(result["telemetry"]["timestampSubject"], "furia.gzsitl.state.drone-1")
        self.assertAlmostEqual(result["telemetry"]["simulationTimeSeconds"], 1.1)
        self.assertAlmostEqual(result["telemetry"]["simulationTimeAdvanceSeconds"], 0.1)

    @patch(
        "interactive_profile.process_identity",
        side_effect=[
            "owned-process --qgc-peer 127.0.0.1:14550 --qgc-bind 127.0.0.1:14651",
            "qgc-process",
        ],
    )
    def test_status_rejects_nonadvancing_gazebo_time(self, _identity) -> None:
        telemetry = {
            "schema": "furia.telemetry",
            "version": "1.0.0",
            "protocol": "antoine-gz",
            "identity": "drone-1",
        }
        state = {
            "schema": "furia.gzsitl.state",
            "version": "1.1.0",
            "identity": "drone-1",
            "simulation_time_ns": 1_000_000_000,
        }
        with patch("interactive_profile.subprocess.run") as run:

            def result_for(command, **_kwargs):
                result = type("Result", (), {"returncode": 0, "stdout": ""})()
                if command[0] == "nats":
                    sample = state if command[-1] == "furia.gzsitl.state.drone-1" else telemetry
                    result.stdout = f"{json.dumps(sample)}\n{json.dumps(sample)}\n"
                return result

            run.side_effect = result_for
            result = interactive_profile.inspect(self.receipt)
        self.assertEqual(result["telemetry"]["status"], "stale")
        self.assertIsNone(result["armed"])

    @patch(
        "interactive_profile.process_identity",
        side_effect=[
            "owned-process --qgc-peer 127.0.0.1:14550 --qgc-bind 127.0.0.1:14651",
            "qgc-process",
        ],
    )
    def test_status_rejects_telemetry_for_another_callsign(self, _identity) -> None:
        with patch("interactive_profile.subprocess.run") as run:

            def result_for(command, **_kwargs):
                result = type("Result", (), {"returncode": 0, "stdout": ""})()
                if command[0] == "nats":
                    foreign = {
                        "schema": "furia.telemetry",
                        "version": "1.0.0",
                        "protocol": "antoine",
                        "identity": "other-drone",
                        "time_s": 1.0,
                        "armed": True,
                    }
                    later = {**foreign, "time_s": 1.1}
                    result.stdout = f"{json.dumps(foreign)}\n{json.dumps(later)}\n"
                return result

            run.side_effect = result_for
            result = interactive_profile.inspect(self.receipt)
        self.assertEqual(result["telemetry"]["status"], "invalid")
        self.assertIsNone(result["armed"])
        self.assertEqual(result["status"], "partial")

    @patch(
        "interactive_profile.process_identity",
        side_effect=[
            "owned-process --qgc-peer 127.0.0.1:14550 --qgc-bind 127.0.0.1:14651",
            "qgc-process",
        ],
    )
    def test_status_rejects_nonadvancing_simulation_time(self, _identity) -> None:
        telemetry = {
            "schema": "furia.telemetry",
            "version": "1.0.0",
            "protocol": "antoine-gz",
            "identity": "drone-1",
            "time_s": 12.5,
            "armed": False,
        }
        with patch("interactive_profile.subprocess.run") as run:

            def result_for(command, **_kwargs):
                result = type("Result", (), {"returncode": 0, "stdout": ""})()
                if command[0] == "nats":
                    result.stdout = f"{json.dumps(telemetry)}\n{json.dumps(telemetry)}\n"
                return result

            run.side_effect = result_for
            result = interactive_profile.inspect(self.receipt)
        self.assertEqual(result["telemetry"]["status"], "stale")
        self.assertIsNone(result["armed"])
        self.assertEqual(result["status"], "partial")

    @patch("interactive_profile.process_identity", return_value=None)
    def test_status_reports_confirmed_stop_without_claiming_current_telemetry(
        self, _identity
    ) -> None:
        receipt = json.loads(self.receipt.read_text(encoding="utf-8"))
        self.add_supervisor_process(receipt)
        self.receipt.write_text(json.dumps(receipt), encoding="utf-8")
        with patch("interactive_profile.subprocess.run") as run:
            run.return_value.returncode = 1
            result = interactive_profile.inspect(self.receipt)
        self.assertEqual(result["status"], "stopped")
        self.assertEqual(result["telemetry"]["status"], "unavailable")
        self.assertIsNone(result["armed"])

    @patch("interactive_profile.process_identity", return_value=None)
    def test_status_accepts_removed_supervisor_records_after_owned_process_exit(
        self, _identity
    ) -> None:
        receipt = json.loads(self.receipt.read_text(encoding="utf-8"))
        self.add_supervisor_process(receipt)
        self.receipt.write_text(json.dumps(receipt), encoding="utf-8")
        for process in receipt["processes"].values():
            identity_path = process.get("supervisorIdentityPath")
            if identity_path:
                Path(identity_path).unlink()

        with patch("interactive_profile.subprocess.run") as run:
            run.return_value.returncode = 1
            result = interactive_profile.inspect(self.receipt)

        self.assertEqual(result["status"], "stopped")
        self.assertEqual(result["processValidation"]["status"], "valid")

    def test_stop_removes_only_valid_stale_run_owned_supervisor_markers(self) -> None:
        receipt = json.loads(self.receipt.read_text(encoding="utf-8"))
        self.add_supervisor_process(receipt)
        bridge_marker = Path(receipt["processes"]["mavlinkBridge"]["supervisorIdentityPath"])
        bridge_fifo = bridge_marker.with_name(
            bridge_marker.name + ".control.0123456789abcdef0123456789abcdef"
        )
        os.mkfifo(bridge_fifo, 0o600)
        self.receipt.write_text(json.dumps(receipt), encoding="utf-8")

        with patch("interactive_profile.process_identity", return_value=None):
            result = interactive_profile.stop_owned(self.receipt)

        self.assertEqual(result["status"], "stopped")
        self.assertEqual(result["results"]["mavlinkBridge"], "already-stopped-stale-marker-cleaned")
        self.assertFalse(bridge_marker.exists())
        self.assertFalse(bridge_fifo.exists())

    def test_stop_preserves_mismatched_stale_marker_and_reports_partial(self) -> None:
        receipt = json.loads(self.receipt.read_text(encoding="utf-8"))
        self.add_supervisor_process(receipt)
        marker = Path(receipt["processes"]["mavlinkBridge"]["supervisorIdentityPath"])
        marker.write_text(
            marker.read_text(encoding="utf-8").replace("child_pid=9001", "child_pid=9999"),
            encoding="utf-8",
        )
        self.receipt.write_text(json.dumps(receipt), encoding="utf-8")

        with patch("interactive_profile.process_identity", return_value=None):
            result = interactive_profile.stop_owned(self.receipt)

        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["results"]["mavlinkBridge"], "stale-marker-not-cleaned")
        self.assertTrue(marker.exists())

    @patch("interactive_profile.process_identity", side_effect=["foreign-process", "qgc-process"])
    @patch("interactive_profile.os.kill")
    def test_stop_never_signals_reused_pid_or_user_qgc(self, kill, _identity) -> None:
        result = interactive_profile.stop_owned(self.receipt)
        kill.assert_not_called()
        self.assertEqual(result["results"]["mavlinkBridge"], "identity-mismatch-not-signalled")
        self.assertEqual(result["results"]["qgcUserProcess"], "preserved-user-owned")

    @patch("interactive_profile.process_identity", return_value="foreign-process")
    def test_stop_cli_returns_failure_for_partial_cleanup(self, _identity) -> None:
        output = io.StringIO()
        with (
            patch.object(sys, "argv", ["interactive_profile.py", "stop", str(self.receipt)]),
            contextlib.redirect_stdout(output),
        ):
            exit_code = interactive_profile.main()

        self.assertEqual(exit_code, 1)
        stdout_report = json.loads(output.getvalue())
        persisted_report = json.loads(
            (Path(self.temp.name) / "interactive-profile-stop-result.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(stdout_report["status"], "partial")
        self.assertEqual(persisted_report["status"], stdout_report["status"])

    def test_later_partial_stop_replaces_previous_success_report(self) -> None:
        receipt = json.loads(self.receipt.read_text(encoding="utf-8"))
        self.add_supervisor_process(receipt)
        self.receipt.write_text(json.dumps(receipt), encoding="utf-8")
        report_path = Path(self.temp.name) / "interactive-profile-stop-result.json"
        with patch("interactive_profile.process_identity", return_value=None):
            first = interactive_profile.stop_owned(self.receipt)
        self.assertEqual(first["status"], "stopped")

        with patch("interactive_profile.process_identity", return_value="foreign-process"):
            second = interactive_profile.stop_owned(self.receipt)

        persisted_report = json.loads(report_path.read_text(encoding="utf-8"))
        self.assertEqual(second["status"], "partial")
        self.assertEqual(persisted_report["status"], "partial")
        self.assertEqual(
            persisted_report["results"]["mavlinkBridge"], "identity-mismatch-not-signalled"
        )

    def test_stop_does_not_report_success_when_no_run_owned_processes_are_recorded(self) -> None:
        receipt = json.loads(self.receipt.read_text(encoding="utf-8"))
        receipt["processes"] = {"qgcUserProcess": {"pid": 9002, "identity": "qgc-process"}}
        self.receipt.write_text(json.dumps(receipt), encoding="utf-8")
        with patch("interactive_profile.process_identity") as process_identity:
            result = interactive_profile.stop_owned(self.receipt)

        process_identity.assert_not_called()
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["results"]["processes"], "no-run-owned-processes-recorded")

    def test_stop_does_not_report_success_for_empty_process_records(self) -> None:
        receipt = json.loads(self.receipt.read_text(encoding="utf-8"))
        receipt["processes"] = {}
        self.receipt.write_text(json.dumps(receipt), encoding="utf-8")
        with patch("interactive_profile.process_identity") as process_identity:
            result = interactive_profile.stop_owned(self.receipt)

        process_identity.assert_not_called()
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["results"]["processes"], "no-run-owned-processes-recorded")

    def test_stop_rejects_run_owned_process_record_without_pid_or_identity(self) -> None:
        receipt = json.loads(self.receipt.read_text(encoding="utf-8"))
        receipt["processes"] = {"mavlinkBridge": {"pid": None, "identity": ""}}
        self.receipt.write_text(json.dumps(receipt), encoding="utf-8")
        with (
            patch("interactive_profile.process_identity") as process_identity,
            patch("interactive_profile.os.kill") as kill,
        ):
            result = interactive_profile.stop_owned(self.receipt)

        process_identity.assert_not_called()
        kill.assert_not_called()
        self.assertEqual(result["status"], "partial")
        self.assertEqual(
            result["results"]["mavlinkBridge"], "invalid-process-identity-not-signalled"
        )

    def test_stop_reports_partial_when_a_run_owned_port_remains_open(self) -> None:
        self.port_status_mock.return_value = {
            "natsPort": "open",
            "bridgeUdpBindPort": "closed",
        }
        with patch("interactive_profile.process_identity", return_value=None):
            result = interactive_profile.stop_owned(self.receipt)

        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["results"]["natsPort"], "open")

    @patch(
        "interactive_profile.process_identity",
        return_value="owned-process --qgc-peer 127.0.0.1:14550 --qgc-bind 127.0.0.1:14651",
    )
    def test_stop_requests_run_local_supervisor_and_waits_for_owned_process(self, _identity):
        with (
            patch(
                "interactive_profile.subprocess.run",
                return_value=type("Result", (), {"returncode": 0, "stdout": "", "stderr": ""})(),
            ) as run,
            patch("interactive_profile._wait_for_owned_exit", return_value="stopped"),
        ):
            result = interactive_profile.stop_owned(self.receipt)

        self.assertEqual(result["results"]["mavlinkBridge"], "stopped")
        self.assertEqual(result["results"]["qgcUserProcess"], "preserved-user-owned")
        args = run.call_args.args[0]
        self.assertEqual(args[1], "stop")
        self.assertEqual(args[2], str((Path(self.temp.name) / ".bridge.identity").resolve()))

    @patch("interactive_profile.time.sleep")
    @patch(
        "interactive_profile.process_identity",
        side_effect=["drone-1", "drone-1", "drone-1"],
    )
    def test_stop_waits_for_run_local_supervisor_identity_record(self, _identity, _sleep) -> None:
        receipt = json.loads(self.receipt.read_text(encoding="utf-8"))
        identity_path = Path(self.temp.name) / ".bridge.identity"
        receipt["processes"]["mavlinkBridge"]["supervisorIdentityPath"] = str(identity_path)
        identity_path.write_text("pid=8991\nchild_pid=9001\nfifo=/tmp/bridge.control\n")
        identity_path.chmod(0o600)
        receipt["processes"]["mavlinkBridge"]["identity"] = "drone-1"
        self.receipt.write_text(json.dumps(receipt), encoding="utf-8")

        with (
            patch(
                "interactive_profile._supervisor_identity_path",
                side_effect=[None, None, None, None, identity_path],
            ),
            patch("interactive_profile._stop_supervised_process", return_value="stopped") as stop,
        ):
            result = interactive_profile.stop_owned(self.receipt)

        self.assertEqual(result["results"]["mavlinkBridge"], "stopped")
        stop.assert_called_once()

    def test_completed_supervisor_cleanup_accepts_pid_reuse_after_control_record_removal(
        self,
    ) -> None:
        identity_path = Path(self.temp.name) / ".bridge.identity"
        identity_path.write_text("run-owned supervisor record\n", encoding="utf-8")
        module_root = Path(self.temp.name) / "module"
        module_root.mkdir()
        supervisor = module_root / ".process-supervisor"
        supervisor.write_text("test supervisor stub\n", encoding="utf-8")
        supervisor.chmod(0o700)
        self.addCleanup(lambda: supervisor.unlink(missing_ok=True))

        def complete_stop(*_args, **_kwargs):
            identity_path.unlink()
            return type("Result", (), {"returncode": 0, "stdout": "", "stderr": ""})()

        with (
            patch("interactive_profile.__file__", str(module_root / "interactive_profile.py")),
            patch("interactive_profile.subprocess.run", side_effect=complete_stop),
            patch(
                "interactive_profile._wait_for_owned_exit",
                return_value="identity-changed-after-interrupt",
            ),
        ):
            result = interactive_profile._stop_supervised_process(
                9001, "original process identity", identity_path
            )

        self.assertEqual(result, "stopped")

    def test_stop_waits_for_supervisor_marker_after_child_identity_changes(self) -> None:
        identity_path = Path(self.temp.name) / ".bridge.identity"
        identity_path.write_text("run-owned supervisor record\n", encoding="utf-8")
        module_root = Path(self.temp.name) / "module"
        module_root.mkdir()
        supervisor = module_root / ".process-supervisor"
        supervisor.write_text("test supervisor stub\n")
        supervisor.chmod(0o700)
        self.addCleanup(lambda: supervisor.unlink(missing_ok=True))

        def request_stop(*_args, **_kwargs):
            return type("Result", (), {"returncode": 0, "stdout": "", "stderr": ""})()

        with (
            patch("interactive_profile.__file__", str(module_root / "interactive_profile.py")),
            patch("interactive_profile.subprocess.run", side_effect=request_stop),
            patch(
                "interactive_profile._wait_for_owned_exit",
                return_value="identity-changed-after-interrupt",
            ),
            patch(
                "interactive_profile.time.sleep",
                side_effect=lambda _seconds: identity_path.unlink(missing_ok=True),
            ),
        ):
            result = interactive_profile._stop_supervised_process(
                9002, "original process identity", identity_path
            )

        self.assertEqual(result, "stopped")

    @patch(
        "interactive_profile.process_identity",
        return_value="owned-process --qgc-peer 127.0.0.1:14550 --qgc-bind 127.0.0.1:14651",
    )
    def test_stop_rejects_supervisor_identity_outside_run_without_signaling(self, _identity):
        receipt = json.loads(self.receipt.read_text(encoding="utf-8"))
        receipt["processes"]["mavlinkBridge"]["supervisorIdentityPath"] = str(
            Path(self.temp.name).parent / "foreign.identity"
        )
        self.receipt.write_text(json.dumps(receipt), encoding="utf-8")
        with patch("interactive_profile.subprocess.run") as run:
            result = interactive_profile.stop_owned(self.receipt)

        run.assert_not_called()
        self.assertEqual(result["status"], "partial")
        self.assertEqual(
            result["results"]["mavlinkBridge"], "supervisor-identity-required-not-signalled"
        )

    @patch(
        "interactive_profile.process_identity",
        return_value="owned-process --qgc-peer 127.0.0.1:14550 --qgc-bind 127.0.0.1:14651",
    )
    def test_legacy_process_record_fails_closed_without_supervisor_identity(self, _identity):
        receipt = json.loads(self.receipt.read_text(encoding="utf-8"))
        del receipt["processes"]["mavlinkBridge"]["supervisorIdentityPath"]
        self.receipt.write_text(json.dumps(receipt), encoding="utf-8")
        with patch("interactive_profile.subprocess.run") as run:
            result = interactive_profile.stop_owned(self.receipt)

        run.assert_not_called()
        self.assertEqual(result["status"], "partial")
        self.assertEqual(
            result["results"]["mavlinkBridge"], "supervisor-identity-required-not-signalled"
        )

    @patch("interactive_profile.process_identity", side_effect=OSError("ps unavailable"))
    def test_stop_persists_partial_when_process_identity_probe_fails(self, _identity) -> None:
        result = interactive_profile.stop_owned(self.receipt)

        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["results"]["mavlinkBridge"], "identity-probe-unavailable")
        persisted = json.loads(
            (Path(self.temp.name) / "interactive-profile-stop-result.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(persisted["status"], "partial")

    def test_run_owned_port_probe_excludes_qgc_and_distinguishes_open_ports(self) -> None:
        with patch("interactive_profile.subprocess.run") as run:
            run.side_effect = [
                type("Result", (), {"returncode": 1})(),
                type("Result", (), {"returncode": 0})(),
                type("Result", (), {"returncode": 1})(),
            ]
            result = self.real_port_status(json.loads(self.receipt.read_text(encoding="utf-8")))

        self.assertEqual(
            result,
            {
                "natsPort": "closed",
                "bridgeUdpBindPort": "open",
                "qgcUdpListenPort": "closed",
            },
        )
        commands = [call.args[0] for call in run.call_args_list]
        self.assertEqual(len(commands), 3)
        self.assertIn("-iTCP:14226", commands[0])
        self.assertIn("-iUDP:14651", commands[1])
        self.assertIn("-iUDP:14550", commands[2])

    @patch("interactive_profile.process_identity")
    @patch("interactive_profile.os.kill")
    def test_stop_rejects_receipt_with_mismatched_run_identity(
        self, kill, process_identity
    ) -> None:
        receipt = json.loads(self.receipt.read_text(encoding="utf-8"))
        receipt["runId"] = "another-run"
        self.receipt.write_text(json.dumps(receipt), encoding="utf-8")

        result = interactive_profile.stop_owned(self.receipt)

        process_identity.assert_not_called()
        kill.assert_not_called()
        self.assertEqual(result["status"], "partial")
        self.assertIn("does not match runId", result["results"]["runReceipt"])
        report = json.loads(
            (Path(self.temp.name) / "interactive-profile-stop-result.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(report["status"], "partial")
        self.assertEqual(report["runId"], "another-run")

    @patch("interactive_profile.process_identity")
    @patch("interactive_profile.os.kill")
    def test_stop_rejects_receipt_for_missing_run_directory(self, kill, process_identity) -> None:
        receipt = json.loads(self.receipt.read_text(encoding="utf-8"))
        receipt["runDirectory"] = str(Path(self.temp.name) / "missing-run")
        receipt["runId"] = "missing-run"
        self.receipt.write_text(json.dumps(receipt), encoding="utf-8")

        result = interactive_profile.stop_owned(self.receipt)

        process_identity.assert_not_called()
        kill.assert_not_called()
        self.assertEqual(result["status"], "partial")
        self.assertIn("cannot be resolved", result["results"]["runReceipt"])

    def test_stop_persists_report_beside_resolved_receipt_target(self) -> None:
        outside = Path(self.temp.name) / "outside"
        outside.mkdir()
        receipt_link = outside / "receipt.json"
        receipt_link.symlink_to(self.receipt)
        receipt = json.loads(self.receipt.read_text(encoding="utf-8"))
        self.add_supervisor_process(receipt)
        self.receipt.write_text(json.dumps(receipt), encoding="utf-8")

        with patch("interactive_profile.process_identity", return_value=None):
            result = interactive_profile.stop_owned(receipt_link)

        self.assertEqual(result["status"], "stopped")
        self.assertTrue((Path(self.temp.name) / "interactive-profile-stop-result.json").exists())
        self.assertFalse((outside / "interactive-profile-stop-result.json").exists())

    @patch(
        "interactive_profile.process_identity",
        return_value="owned-process --qgc-peer 127.0.0.1:14550 --qgc-bind 127.0.0.1:14651",
    )
    def test_supervisor_stop_rejection_preserves_partial_result(self, _identity):
        with patch(
            "interactive_profile.subprocess.run",
            return_value=type("Result", (), {"returncode": 1, "stdout": "", "stderr": "stale"})(),
        ):
            result = interactive_profile.stop_owned(self.receipt)

        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["results"]["mavlinkBridge"], "supervisor-stop-rejected: stale")

    @patch(
        "interactive_profile.process_identity",
        return_value="owned-process --qgc-peer 127.0.0.1:14550 --qgc-bind 127.0.0.1:14651",
    )
    def test_supervisor_stop_request_is_bounded(self, _identity):
        with patch(
            "interactive_profile.subprocess.run",
            side_effect=subprocess.TimeoutExpired(
                "process-supervisor", interactive_profile.SUPERVISOR_REQUEST_TIMEOUT_SECONDS
            ),
        ) as run:
            result = interactive_profile.stop_owned(self.receipt)

        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["results"]["mavlinkBridge"], "supervisor-stop-timeout")
        self.assertEqual(
            run.call_args.kwargs["timeout"], interactive_profile.SUPERVISOR_REQUEST_TIMEOUT_SECONDS
        )


if __name__ == "__main__":
    unittest.main()
