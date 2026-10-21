from __future__ import annotations

import re
import unittest
from pathlib import Path

PATCH = Path(__file__).resolve().parent / "patches" / "qgroundcontrol-antoine.patch"


class QGroundControlPatchContractTests(unittest.TestCase):
    def test_guided_position_target_uses_radial_three_meter_limit(self) -> None:
        patch = PATCH.read_text(encoding="utf-8")
        match = re.search(r"^\+\s*if \((distance[^\n]+)\) \{$", patch, re.MULTILINE)
        self.assertIsNotNone(match, "guided-position safety guard is missing")
        guard = match.group(1)
        self.assertIn("distance > 3.0", guard)
        self.assertIn("relativeAltitude < 0.0", guard)
        self.assertIn("relativeAltitude > 3.0", guard)
        self.assertNotIn("qAbs(north)", guard)
        self.assertNotIn("qAbs(east)", guard)


if __name__ == "__main__":
    unittest.main()
