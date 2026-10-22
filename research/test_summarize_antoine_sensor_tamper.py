from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from summarize_antoine_sensor_tamper import read, read_sensor_profile


class SensorTamperSummaryTests(unittest.TestCase):
    def test_duplicate_evidence_keys_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "evidence.json"
            path.write_text('{"runId":"run-a","runId":"run-b"}')
            self.assertEqual(read(path), {})

    def test_duplicate_sensor_profile_keys_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sensor-profile.json"
            path.write_text('{"sourceModified":false,"sourceModified":true}\nGZ_PARTITION=p\n')
            self.assertEqual(read_sensor_profile(path), {})


if __name__ == "__main__":
    unittest.main()
