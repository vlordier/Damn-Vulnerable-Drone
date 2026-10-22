from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

import antoine_setpoint_mutation


class AntoineSetpointPayloadTests(unittest.TestCase):
    def test_raw_setpoint_decoder_rejects_duplicate_run_identity(self) -> None:
        with self.assertRaisesRegex(ValueError, "NATS raw subscription"):
            antoine_setpoint_mutation.read_one_json('{"runId":"stale-run","runId":"selected-run"}')

    def test_actuator_audit_skips_duplicate_authority_records(self) -> None:
        valid = (
            '{"stage":"explicit-stop","neutral":true,"runId":"run-1",'
            '"contract":"furia.gzsitl.actuator-output-audit",'
            '"actuatorKind":"motors","vehicleId":"drone-1",'
            '"authority":"safe-stop"}'
        )
        ambiguous = valid.replace(
            '"authority":"safe-stop"', '"authority":"bad","authority":"safe-stop"'
        )
        with patch.object(
            antoine_setpoint_mutation,
            "read_run_text",
            return_value=f"{ambiguous}\n{valid}\n",
        ):
            rows = antoine_setpoint_mutation.read_audit_rows(Path("/run"))

        self.assertEqual(
            rows,
            [
                {
                    "stage": "explicit-stop",
                    "neutral": True,
                    "runId": "run-1",
                    "contract": "furia.gzsitl.actuator-output-audit",
                    "actuatorKind": "motors",
                    "vehicleId": "drone-1",
                    "authority": "safe-stop",
                }
            ],
        )


if __name__ == "__main__":
    unittest.main()
