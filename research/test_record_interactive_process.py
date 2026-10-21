"""Tests for verified interactive profile process recording."""

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from record_interactive_process import record_process


class RecordInteractiveProcessTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.receipt_path = Path(self.tempdir.name) / "receipt.json"
        self.original = {"processes": {}}
        self.receipt_path.write_text(json.dumps(self.original))

    def tearDown(self):
        self.tempdir.cleanup()

    @patch("record_interactive_process.subprocess.run")
    def test_records_successful_identity_and_ready_metadata(self, run):
        run.return_value = subprocess.CompletedProcess([], 0, "Mon Oct 20 process command\n", "")

        record_process(
            self.receipt_path,
            "supervisor",
            42,
            status="ready",
            run_directory=self.tempdir.name,
        )

        receipt = json.loads(self.receipt_path.read_text())
        self.assertEqual(
            receipt["processes"]["supervisor"],
            {
                "pid": 42,
                "identity": "Mon Oct 20 process command",
            },
        )
        self.assertEqual(receipt["status"], "ready")
        self.assertEqual(receipt["runDirectory"], str(Path(self.tempdir.name).resolve()))

    @patch("record_interactive_process.subprocess.run")
    def test_nonzero_ps_result_does_not_modify_receipt(self, run):
        run.return_value = subprocess.CompletedProcess([], 1, "", "no such process")

        with self.assertRaisesRegex(ValueError, "no such process"):
            record_process(self.receipt_path, "bridge", 42)

        self.assertEqual(json.loads(self.receipt_path.read_text()), self.original)

    @patch("record_interactive_process.subprocess.run")
    def test_empty_ps_identity_does_not_modify_receipt(self, run):
        run.return_value = subprocess.CompletedProcess([], 0, " \n", "")

        with self.assertRaisesRegex(ValueError, "cannot record process identity"):
            record_process(self.receipt_path, "bridge", 42)

        self.assertEqual(json.loads(self.receipt_path.read_text()), self.original)

    @patch("record_interactive_process.subprocess.run")
    def test_invalid_receipt_does_not_get_replaced(self, run):
        run.return_value = subprocess.CompletedProcess([], 0, "identity", "")
        self.receipt_path.write_text('{"processes": null}')
        original_text = self.receipt_path.read_text()

        with self.assertRaisesRegex(ValueError, "processes object"):
            record_process(self.receipt_path, "bridge", 42)

        self.assertEqual(self.receipt_path.read_text(), original_text)

    def test_rejects_invalid_pid_before_probing(self):
        with patch("record_interactive_process.subprocess.run") as run:
            with self.assertRaisesRegex(ValueError, "PID"):
                record_process(self.receipt_path, "bridge", 1)
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
