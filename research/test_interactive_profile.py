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
        self.receipt = Path(self.temp.name) / "interactive-profile-receipt.json"
        self.receipt.write_text(
            json.dumps(
                {
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

    @patch("interactive_profile.process_identity", return_value="foreign-process")
    def test_status_marks_pid_reuse_as_partial_without_attributing_process(self, _identity) -> None:
        with patch("interactive_profile.subprocess.run") as run:
            run.return_value.returncode = 0
            result = interactive_profile.inspect(self.receipt)
        self.assertEqual(result["status"], "partial")
        self.assertFalse(result["processes"]["mavlinkBridge"]["identityMatches"])
        self.assertIsNone(result["armed"])

    @patch(
        "interactive_profile.process_identity",
        side_effect=[
            "owned-process --qgc-peer 127.0.0.1:14550 --qgc-bind 127.0.0.1:14651",
            "qgc-process",
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
        self.assertEqual(json.loads(output.getvalue())["status"], "partial")

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
        self.assertEqual(result["results"]["runReceipt"], "invalid-run-identity-not-signalled")
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
        self.assertEqual(result["results"]["runReceipt"], "invalid-run-identity-not-signalled")

    @patch(
        "interactive_profile.process_identity",
        side_effect=[
            "owned-process --qgc-peer 127.0.0.1:14550 --qgc-bind 127.0.0.1:14651",
            None,
        ],
    )
    @patch("interactive_profile.os.kill")
    def test_stop_interrupts_only_identity_matched_owned_child(self, kill, _identity) -> None:
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
        return_value="owned-process --qgc-peer 127.0.0.1:14550 --qgc-bind 127.0.0.1:14651",
    )
    @patch("interactive_profile._wait_for_owned_exit", side_effect=["timeout", "stopped"])
    @patch("interactive_profile.os.kill")
    def test_stop_escalates_to_term_after_interrupt_timeout(
        self, kill, _wait_for_exit, _identity
    ) -> None:
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
    def test_stop_waits_for_exit_after_interrupt_identity_change_without_resignalling(
        self, kill, _identity
    ) -> None:
        result = interactive_profile.stop_owned(self.receipt)
        kill.assert_called_once_with(9001, interactive_profile.signal.SIGINT)
        self.assertEqual(result["status"], "stopped")
        self.assertEqual(result["results"]["mavlinkBridge"], "stopped-after-identity-change")


if __name__ == "__main__":
    unittest.main()
