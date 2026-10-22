from __future__ import annotations

import unittest

from dvd_research.nats_identity import valid_callsign


class NatsIdentityTests(unittest.TestCase):
    def test_accepts_supported_ascii_vehicle_tokens(self) -> None:
        for value in ("drone-1", "antoine_sitl", "A9"):
            with self.subTest(value=value):
                self.assertTrue(valid_callsign(value))

    def test_rejects_subject_injection_and_invalid_lengths(self) -> None:
        for value in ("", "*", ">", "drone.1", "drone/1", "two words", "é", "x" * 65):
            with self.subTest(value=value):
                self.assertFalse(valid_callsign(value))

    def test_accepts_maximum_length(self) -> None:
        self.assertTrue(valid_callsign("x" * 64))


if __name__ == "__main__":
    unittest.main()
