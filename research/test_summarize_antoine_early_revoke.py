from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from summarize_antoine_early_revoke import read_actuator_audit


class EarlyRevokeAuditTests(unittest.TestCase):
    def test_duplicate_key_actuator_row_invalidates_entire_jsonl(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "actuator-output.jsonl"
            path.write_text(
                '{"stage":"explicit-stop","neutral":true}\n'
                '{"stage":"explicit-stop","neutral":true,"neutral":false}\n'
            )

            rows, valid = read_actuator_audit(path)

        self.assertFalse(valid)
        self.assertEqual(rows, [{"stage": "explicit-stop", "neutral": True}])

    def test_non_object_actuator_row_invalidates_entire_jsonl(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "actuator-output.jsonl"
            path.write_text('{"stage":"explicit-stop"}\nnull\n')

            rows, valid = read_actuator_audit(path)

        self.assertFalse(valid)
        self.assertEqual(rows, [{"stage": "explicit-stop"}])

    def test_invalid_utf8_actuator_audit_is_disqualifying(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "actuator-output.jsonl"
            path.write_bytes(b'{"stage":"explicit-stop"}\n\xff\n')

            rows, valid = read_actuator_audit(path)

        self.assertFalse(valid)
        self.assertEqual(rows, [])


if __name__ == "__main__":
    unittest.main()
