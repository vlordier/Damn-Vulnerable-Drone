from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from antoine_gps_tamper import (
    ACCEPTED_MARKER,
    count_accepted,
    read_log,
    validate_gps_numeric_fields,
)


class GpsTamperEvidenceTests(unittest.TestCase):
    def test_captured_gps_numeric_fields_reject_booleans_and_invalid_ranges(self) -> None:
        valid = {
            "timestamp_ms": 1_000,
            "expires_at_ms": 2_000,
            "sequence": 1,
            "lat_deg": 48.5,
        }
        self.assertEqual(validate_gps_numeric_fields(valid), (1_000, 2_000, 1, 48.5))
        for field, value in (
            ("timestamp_ms", True),
            ("expires_at_ms", False),
            ("sequence", True),
            ("lat_deg", True),
            ("lat_deg", float("nan")),
            ("lat_deg", float("inf")),
            ("lat_deg", 91.0),
        ):
            with self.subTest(field=field, value=value):
                invalid = {**valid, field: value}
                with self.assertRaises(ValueError):
                    validate_gps_numeric_fields(invalid)

    def test_captured_gps_sample_requires_expiry_after_timestamp(self) -> None:
        for timestamps in ((0, 0), (2_000, 1_000)):
            with self.subTest(timestamps=timestamps):
                payload = {
                    "timestamp_ms": timestamps[0],
                    "expires_at_ms": timestamps[1],
                    "sequence": 1,
                    "lat_deg": 48.5,
                }
                with self.assertRaisesRegex(ValueError, "freshness timestamps"):
                    validate_gps_numeric_fields(payload)

    def test_controller_acceptance_count_reads_the_run_owned_log(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory)
            logs = run_dir / "logs"
            logs.mkdir()
            (logs / "antoine-controller.log").write_text(
                f"startup\n{ACCEPTED_MARKER}\n{ACCEPTED_MARKER}\n", encoding="utf-8"
            )
            self.assertEqual(count_accepted(run_dir), 2)

    def test_log_reader_rejects_symlinked_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory) / "run"
            run_dir.mkdir()
            external = Path(directory) / "external.log"
            external.write_text(ACCEPTED_MARKER, encoding="utf-8")
            (run_dir / "controller.log").symlink_to(external)
            with self.assertRaisesRegex(ValueError, "symlink"):
                read_log(run_dir, Path("controller.log"))

    def test_missing_log_is_pending(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(read_log(Path(directory), Path("logs/controller.log")), "")


if __name__ == "__main__":
    unittest.main()
