from __future__ import annotations

import json
import unittest

from dvd_research.strict_json import loads as strict_json_loads
from dvd_research.strict_json import raw_decode


class StrictNatsJsonTests(unittest.TestCase):
    def test_accepts_regular_nested_json(self) -> None:
        self.assertEqual(
            strict_json_loads(b'{"items":[{"identity":"drone-1"}],"armed":false}'),
            {"items": [{"identity": "drone-1"}], "armed": False},
        )

    def test_rejects_duplicate_keys_at_every_depth(self) -> None:
        for payload in (
            b'{"identity":"other","identity":"drone-1"}',
            b'{"nested":{"runId":"old","runId":"selected"}}',
            b'{"items":[{"accepted":false,"accepted":true}]}',
            b'{"identity":"other","\\u0069dentity":"drone-1"}',
        ):
            with (
                self.subTest(payload=payload),
                self.assertRaisesRegex(json.JSONDecodeError, "duplicate object key"),
            ):
                strict_json_loads(payload)

    def test_rejects_nonstandard_numeric_constants(self) -> None:
        for payload in (b'{"value":NaN}', b'{"value":Infinity}', b'{"value":-Infinity}'):
            with (
                self.subTest(payload=payload),
                self.assertRaisesRegex(json.JSONDecodeError, "non-standard JSON constant"),
            ):
                strict_json_loads(payload)

    def test_rejects_numeric_overflow_in_loads_and_stream_decoder(self) -> None:
        for payload in ('{"value":1e999}', '{"value":-1e999}'):
            with (
                self.subTest(payload=payload),
                self.assertRaisesRegex(json.JSONDecodeError, "outside finite floating-point range"),
            ):
                strict_json_loads(payload)

            with (
                self.subTest(raw_payload=payload),
                self.assertRaisesRegex(json.JSONDecodeError, "outside finite floating-point range"),
            ):
                raw_decode(payload)


if __name__ == "__main__":
    unittest.main()
