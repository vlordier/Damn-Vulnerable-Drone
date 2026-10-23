from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from dvd_research.sensor_fixture import fixture_signer


class SensorFixtureTests(unittest.TestCase):
    def test_verifying_key_registry_rejects_duplicate_json_keys(self) -> None:
        with patch.dict(
            os.environ,
            {
                "FURIA_GZ_SENSOR_SIGNING_SEED_HEX": "00" * 32,
                "FURIA_SENSOR_VERIFYING_KEYS_JSON": '{"key":"wrong","key":"right"}',
            },
        ):
            with self.assertRaisesRegex(ValueError, "run-local verifying-key registry"):
                fixture_signer()


if __name__ == "__main__":
    unittest.main()
