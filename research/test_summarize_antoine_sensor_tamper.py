from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from summarize_antoine_sensor_tamper import accepted_trace_delta_is, read, read_sensor_profile


class SensorTamperSummaryTests(unittest.TestCase):
    def test_trace_delta_gate_requires_exact_integer_measurement(self) -> None:
        self.assertTrue(accepted_trace_delta_is({"acceptedGpsTraceDelta": 0}, 0))
        self.assertTrue(accepted_trace_delta_is({"acceptedGpsTraceDelta": 1}, 1))
        for value in (False, True, 0.0, 1.0, "1", None):
            with self.subTest(value=value):
                self.assertFalse(accepted_trace_delta_is({"acceptedGpsTraceDelta": value}, 1))

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
