from __future__ import annotations

import subprocess
import unittest
from unittest import mock

from check_udp_loopback import _application_firewall_blocks_python, local_udp_loopback_available


class MacFirewallPreflightTests(unittest.TestCase):
    def test_detects_a_block_rule_for_the_resolved_interpreter(self) -> None:
        result = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="Incoming connection is blocked.\n", stderr=""
        )
        with (
            mock.patch("check_udp_loopback.sys.platform", "darwin"),
            mock.patch("check_udp_loopback.Path.exists", return_value=True),
            mock.patch("check_udp_loopback.Path.resolve", return_value="/resolved/python"),
            mock.patch("check_udp_loopback.subprocess.run", return_value=result) as run,
        ):
            self.assertTrue(_application_firewall_blocks_python())

        self.assertEqual(run.call_args.args[0][-1], "/resolved/python")

    def test_permitted_rule_does_not_block_the_udp_probe(self) -> None:
        result = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="Incoming connection is permitted.\n", stderr=""
        )
        with (
            mock.patch("check_udp_loopback.sys.platform", "darwin"),
            mock.patch("check_udp_loopback.Path.exists", return_value=True),
            mock.patch("check_udp_loopback.subprocess.run", return_value=result),
        ):
            self.assertFalse(_application_firewall_blocks_python())

    def test_explicit_not_blocked_status_does_not_block_the_udp_probe(self) -> None:
        result = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="Incoming connection is not blocked.\n", stderr=""
        )
        with (
            mock.patch("check_udp_loopback.sys.platform", "darwin"),
            mock.patch("check_udp_loopback.Path.exists", return_value=True),
            mock.patch("check_udp_loopback.Path.resolve", return_value="/resolved/python"),
            mock.patch("check_udp_loopback.subprocess.run", return_value=result),
        ):
            self.assertFalse(_application_firewall_blocks_python())

    def test_firewall_query_failure_does_not_override_the_udp_probe(self) -> None:
        result = subprocess.CompletedProcess(
            args=[], returncode=1, stdout="Incoming connection is blocked.\n", stderr="error"
        )
        with (
            mock.patch("check_udp_loopback.sys.platform", "darwin"),
            mock.patch("check_udp_loopback.Path.exists", return_value=True),
            mock.patch("check_udp_loopback.Path.resolve", return_value="/resolved/python"),
            mock.patch("check_udp_loopback.subprocess.run", return_value=result),
        ):
            self.assertFalse(_application_firewall_blocks_python())

    def test_successful_loopback_probe_is_authoritative_over_firewall_status(self) -> None:
        receiver = mock.Mock()
        sender = mock.Mock()
        receiver.getsockname.return_value = ("127.0.0.1", 12345)
        receiver.recvfrom.return_value = (b"dvd-udp-loopback-check", ("127.0.0.1", 45678))
        with (
            mock.patch("check_udp_loopback._application_firewall_blocks_python", return_value=True),
            mock.patch(
                "check_udp_loopback.socket.socket", side_effect=[receiver, sender]
            ) as socket_factory,
        ):
            self.assertTrue(local_udp_loopback_available())

        self.assertEqual(socket_factory.call_count, 2)
        sender.sendto.assert_called_once_with(b"dvd-udp-loopback-check", ("127.0.0.1", 12345))
