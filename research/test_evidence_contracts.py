from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from evidence_contracts import read_json_object


class EvidenceContractTests(unittest.TestCase):
    def test_run_evidence_reader_rejects_duplicate_nested_keys(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "evidence.json"
            path.write_text('{"runId":"run-a","details":{"status":1,"status":2}}')

            self.assertEqual(read_json_object(path), {})

    def test_run_evidence_reader_rejects_non_finite_numbers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "evidence.json"
            path.write_text('{"measured":{"altitude":1e999}}')

            self.assertEqual(read_json_object(path), {})

    def test_run_evidence_reader_preserves_valid_objects_and_rejects_non_objects(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "evidence.json"
            path.write_text('{"runId":"run-a"}')
            self.assertEqual(read_json_object(path), {"runId": "run-a"})

            path.write_text("[1, 2, 3]")
            self.assertEqual(read_json_object(path), {})


if __name__ == "__main__":
    unittest.main()
