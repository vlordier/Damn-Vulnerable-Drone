"""Tests for atomic process-identity signaling."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dvd_research import process_identity


class SignalOwnedProcessTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.identity_path = Path(self.tempdir.name) / "process.identity"
        self.identity = {
            "pid": "9001",
            "lstart": "Mon Oct 20 10:00:00 2026",
            "command": "owned-service --run test",
            "pgid": "9001",
        }
        self.identity_path.write_text(json.dumps(self.identity), encoding="utf-8")

    @patch("dvd_research.process_identity.platform.system", return_value="Linux")
    @patch(
        "dvd_research.process_identity.inspect",
        return_value={
            "lstart": "Mon Oct 20 10:00:00 2026",
            "command": "exec-handoff-child",
            "pgid": "9001",
        },
    )
    def test_child_signal_allows_exec_handoff_but_uses_pidfd(self, _inspect, _system):
        with (
            patch(
                "dvd_research.process_identity.os.pidfd_open", return_value=38, create=True
            ) as open_pidfd,
            patch(
                "dvd_research.process_identity.signal.pidfd_send_signal",
                create=True,
            ) as send_signal,
            patch("dvd_research.process_identity.os.close") as close_fd,
        ):
            process_identity.signal_owned_child(
                9001, self.identity_path, process_identity.signal.SIGTERM
            )

        open_pidfd.assert_called_once_with(9001, 0)
        send_signal.assert_called_once_with(38, process_identity.signal.SIGTERM)
        close_fd.assert_called_once_with(38)

    @patch("dvd_research.process_identity.platform.system", return_value="Linux")
    @patch(
        "dvd_research.process_identity.inspect",
        return_value={
            "lstart": "Mon Oct 20 10:00:00 2026",
            "command": "owned-service --run test",
            "pgid": "9001",
        },
    )
    def test_child_signal_fails_closed_without_pidfd_support(self, _inspect, _system):
        with (
            patch("dvd_research.process_identity.os.pidfd_open", None, create=True),
            patch(
                "dvd_research.process_identity.signal.pidfd_send_signal",
                None,
                create=True,
            ),
            patch("dvd_research.process_identity.os.kill") as kill,
        ):
            with self.assertRaisesRegex(ValueError, "atomic child signalling is unavailable"):
                process_identity.signal_owned_child(
                    9001, self.identity_path, process_identity.signal.SIGTERM
                )

        kill.assert_not_called()

    def tearDown(self):
        self.tempdir.cleanup()

    @patch("dvd_research.process_identity.platform.system", return_value="Linux")
    @patch(
        "dvd_research.process_identity.inspect",
        return_value={
            "lstart": "Mon Oct 20 10:00:00 2026",
            "command": "owned-service --run test",
            "pgid": "9001",
        },
    )
    def test_uses_pidfd_after_revalidating_identity(self, _inspect, _system):
        with (
            patch(
                "dvd_research.process_identity.os.pidfd_open", return_value=37, create=True
            ) as open_pidfd,
            patch(
                "dvd_research.process_identity.signal.pidfd_send_signal",
                create=True,
            ) as send_signal,
            patch("dvd_research.process_identity.os.close") as close_fd,
        ):
            process_identity.signal_owned_process(
                9001, self.identity_path, process_identity.signal.SIGTERM
            )

        open_pidfd.assert_called_once_with(9001, 0)
        send_signal.assert_called_once_with(37, process_identity.signal.SIGTERM)
        close_fd.assert_called_once_with(37)

    @patch("dvd_research.process_identity.platform.system", return_value="Linux")
    @patch(
        "dvd_research.process_identity.inspect",
        side_effect=[
            {
                "lstart": "Mon Oct 20 10:00:00 2026",
                "command": "owned-service --run test",
                "pgid": "9001",
            },
            {
                "lstart": "Mon Oct 20 10:00:01 2026",
                "command": "unrelated-service",
                "pgid": "9001",
            },
        ],
    )
    def test_identity_change_after_pidfd_open_fails_closed(self, _inspect, _system):
        with (
            patch("dvd_research.process_identity.os.pidfd_open", return_value=37, create=True),
            patch(
                "dvd_research.process_identity.signal.pidfd_send_signal",
                create=True,
            ) as send_signal,
            patch("dvd_research.process_identity.os.close") as close_fd,
        ):
            with self.assertRaisesRegex(ValueError, "changed while opening its pidfd"):
                process_identity.signal_owned_process(
                    9001, self.identity_path, process_identity.signal.SIGTERM
                )

        send_signal.assert_not_called()
        close_fd.assert_called_once_with(37)

    @patch("dvd_research.process_identity.platform.system", return_value="Linux")
    @patch(
        "dvd_research.process_identity.inspect",
        return_value={
            "lstart": "Mon Oct 20 10:00:00 2026",
            "command": "owned-service --run test",
            "pgid": "9001",
        },
    )
    def test_missing_pidfd_support_fails_closed(self, _inspect, _system):
        with (
            patch("dvd_research.process_identity.os.pidfd_open", None, create=True),
            patch("dvd_research.process_identity.signal.pidfd_send_signal", None, create=True),
            patch("dvd_research.process_identity.os.kill") as kill,
        ):
            with self.assertRaisesRegex(ValueError, "atomic process signalling is unavailable"):
                process_identity.signal_owned_process(
                    9001, self.identity_path, process_identity.signal.SIGTERM
                )

        kill.assert_not_called()

    @patch("dvd_research.process_identity.platform.system", return_value="Linux")
    @patch(
        "dvd_research.process_identity.inspect",
        return_value={
            "lstart": "Mon Oct 20 10:00:01 2026",
            "command": "reused-pid-process",
            "pgid": "9001",
        },
    )
    def test_initial_identity_mismatch_never_opens_pidfd(self, _inspect, _system):
        with patch("dvd_research.process_identity.os.pidfd_open", create=True) as open_pidfd:
            with self.assertRaisesRegex(ValueError, "identity differs"):
                process_identity.signal_owned_process(
                    9001, self.identity_path, process_identity.signal.SIGTERM
                )

        open_pidfd.assert_not_called()


if __name__ == "__main__":
    unittest.main()
