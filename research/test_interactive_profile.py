from __future__ import annotations

import json
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

    @patch("interactive_profile.process_identity", side_effect=["foreign-process", "qgc-process"])
    @patch("interactive_profile.os.kill")
    def test_stop_never_signals_reused_pid_or_user_qgc(self, kill, _identity) -> None:
        result = interactive_profile.stop_owned(self.receipt)
        kill.assert_not_called()
        self.assertEqual(result["results"]["mavlinkBridge"], "identity-mismatch-not-signalled")
        self.assertEqual(result["results"]["qgcUserProcess"], "preserved-user-owned")

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


if __name__ == "__main__":
    unittest.main()
