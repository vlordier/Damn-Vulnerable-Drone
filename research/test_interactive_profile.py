from __future__ import annotations

import contextlib
import io
import json
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
            return_value={"natsPort": "closed", "bridgeUdpBindPort": "closed"},
        )
        self.port_status_mock = self.port_status.start()
        self.addCleanup(self.port_status.stop)
        self.receipt = Path(self.temp.name) / "interactive-profile-receipt.json"
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
                        },
                        "qgcUserProcess": {"pid": 9002, "identity": "qgc-process"},
                    },
                }
            ),
            encoding="utf-8",
        )
        (Path(self.temp.name) / "logs").mkdir()
        (Path(self.temp.name) / "logs" / "nats.log").write_text(
            "Listening for client connections on 127.0.0.1:14226\nServer is ready\n",
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_status_rejects_non_object_receipt(self) -> None:
        self.receipt.write_text("[]", encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "receipt must be a JSON object"):
            interactive_profile.inspect(self.receipt)

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
        (flight_directory / "logs").mkdir(parents=True)
        (flight_directory / "logs" / "nats.log").write_text(
            "Listening for client connections on 127.0.0.1:14226\nServer is ready\n",
            encoding="utf-8",
        )
        receipt = json.loads(self.receipt.read_text(encoding="utf-8"))
        receipt["processes"]["antoineGazeboSupervisor"] = {
            "pid": 9003,
            "identity": "supervisor-process",
        }
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
        self.assertEqual(result["telemetry"]["status"], "fresh")
        self.assertEqual(result["telemetry"]["protocol"], "antoine-gz")
        self.assertFalse(result["armed"])
        self.assertEqual(result["status"], "recorded")
        self.assertEqual(result["qgcPeerRouting"]["status"], "configured-unverified")

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
        receipt["processes"]["antoineGazeboSupervisor"] = {
            "pid": 9003,
            "identity": "supervisor-process",
        }
        self.receipt.write_text(json.dumps(receipt), encoding="utf-8")
        with patch("interactive_profile.subprocess.run") as run:
            run.return_value.returncode = 1
            result = interactive_profile.inspect(self.receipt)
        self.assertEqual(result["status"], "stopped")
        self.assertEqual(result["telemetry"]["status"], "unavailable")
        self.assertIsNone(result["armed"])

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
        receipt["processes"]["antoineGazeboSupervisor"] = {
            "pid": 9003,
            "identity": "supervisor-process",
        }
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

    def test_run_owned_port_probe_excludes_qgc_and_distinguishes_open_ports(self) -> None:
        with patch("interactive_profile.subprocess.run") as run:
            run.side_effect = [
                type("Result", (), {"returncode": 1})(),
                type("Result", (), {"returncode": 0})(),
            ]
            result = self.real_port_status(json.loads(self.receipt.read_text(encoding="utf-8")))

        self.assertEqual(result, {"natsPort": "closed", "bridgeUdpBindPort": "open"})
        commands = [call.args[0] for call in run.call_args_list]
        self.assertEqual(len(commands), 2)
        self.assertIn("-iTCP:14226", commands[0])
        self.assertIn("-iUDP:14651", commands[1])
        self.assertNotIn("14550", str(commands))

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

    @patch(
        "interactive_profile.process_identity",
        side_effect=[
            "owned-process --qgc-peer 127.0.0.1:14550 --qgc-bind 127.0.0.1:14651",
            None,
            None,
        ],
    )
    @patch("interactive_profile.os.kill")
    def test_stop_interrupts_only_identity_matched_owned_child(self, kill, _identity) -> None:
        receipt = json.loads(self.receipt.read_text(encoding="utf-8"))
        receipt["processes"]["antoineGazeboSupervisor"] = {
            "pid": 9003,
            "identity": "supervisor-process",
        }
        self.receipt.write_text(json.dumps(receipt), encoding="utf-8")
        result = interactive_profile.stop_owned(self.receipt)
        kill.assert_called_once()
        self.assertEqual(result["status"], "stopped")
        self.assertEqual(result["results"]["mavlinkBridge"], "stopped")
        self.assertEqual(result["results"]["qgcUserProcess"], "preserved-user-owned")
        report = json.loads(
            (Path(self.temp.name) / "interactive-profile-stop-result.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(report["status"], "stopped")
        self.assertTrue(report["observedAtUtc"])

    @patch(
        "interactive_profile.process_identity",
        side_effect=[
            "owned-process --qgc-peer 127.0.0.1:14550 --qgc-bind 127.0.0.1:14651",
            None,
        ],
    )
    @patch("interactive_profile.os.kill")
    def test_stop_cleans_known_child_but_stays_partial_without_supervisor_record(
        self, kill, _identity
    ) -> None:
        result = interactive_profile.stop_owned(self.receipt)

        kill.assert_called_once_with(9001, interactive_profile.signal.SIGINT)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["results"]["mavlinkBridge"], "stopped")
        self.assertEqual(
            result["results"]["requiredProcessRecords"],
            "missing:antoineGazeboSupervisor",
        )

    @patch(
        "interactive_profile.process_identity",
        side_effect=[
            "owned-process --qgc-peer 127.0.0.1:14550 --qgc-bind 127.0.0.1:14651",
            "owned-process --qgc-peer 127.0.0.1:14550 --qgc-bind 127.0.0.1:14651",
            None,
        ],
    )
    @patch("interactive_profile._wait_for_owned_exit", side_effect=["timeout", "stopped"])
    @patch("interactive_profile.os.kill")
    def test_stop_escalates_to_term_after_interrupt_timeout(
        self, kill, _wait_for_exit, _identity
    ) -> None:
        receipt = json.loads(self.receipt.read_text(encoding="utf-8"))
        receipt["processes"]["antoineGazeboSupervisor"] = {
            "pid": 9003,
            "identity": "supervisor-process",
        }
        self.receipt.write_text(json.dumps(receipt), encoding="utf-8")
        result = interactive_profile.stop_owned(self.receipt)
        self.assertEqual(result["status"], "stopped")
        self.assertEqual(kill.call_args_list[0].args, (9001, interactive_profile.signal.SIGINT))
        self.assertEqual(kill.call_args_list[1].args, (9001, interactive_profile.signal.SIGTERM))

    @patch(
        "interactive_profile.process_identity",
        side_effect=[
            "owned-process --qgc-peer 127.0.0.1:14550 --qgc-bind 127.0.0.1:14651",
            "changed",
            None,
        ],
    )
    @patch("interactive_profile.os.kill")
    def test_stop_reports_partial_after_interrupt_identity_change_without_resignalling(
        self, kill, _identity
    ) -> None:
        result = interactive_profile.stop_owned(self.receipt)
        kill.assert_called_once_with(9001, interactive_profile.signal.SIGINT)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["results"]["mavlinkBridge"], "stopped-after-identity-change")


if __name__ == "__main__":
    unittest.main()
