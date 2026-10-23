from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path


class OwnedCleanupTrapTests(unittest.TestCase):
    def test_term_exits_with_signal_status_and_runs_cleanup_once(self) -> None:
        helper = Path(__file__).with_name("owned_process.sh")
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "cleanup.log"
            script = r"""
source "$1"
export CLEANUP_MARKER="$2"
cleanup() { printf 'cleanup\n' >> "$CLEANUP_MARKER"; }
dvd_install_exit_cleanup_traps cleanup
kill -TERM "$$"
printf 'continued\n' >> "$CLEANUP_MARKER"
"""
            result = subprocess.run(
                ["bash", "-c", script, "bash", str(helper), str(marker)],
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(result.returncode, 143, result.stderr)
            self.assertEqual(marker.read_text(), "cleanup\n")


if __name__ == "__main__":
    unittest.main()
