"""Reject unbound, stale and forged final armed-state evidence."""

import json
import tempfile
import unittest
from pathlib import Path

from actuator_lifecycle import disarmed_status
from lifecycle_test_fixtures import build_lifecycle


class LifecycleTests(unittest.TestCase):
    def test_explicit_signed_lifecycle_and_independent_stop_are_measured(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory)
            evidence, rows = build_lifecycle(run_dir, "drone-1")
            result = disarmed_status(run_dir, "drone-1", evidence, rows)
        self.assertEqual(result["status"], "measured")
        self.assertTrue(result["disarmed"])
        self.assertTrue(result["signatureVerified"])

    def test_missing_or_stale_identity_signature_and_sequence_fail_closed(self) -> None:
        for mutation in (
            "signature",
            "run",
            "vehicle",
            "boolean",
            "timestamp",
            "duplicate",
            "order",
            "latest",
            "missing",
            "key",
            "unsigned",
        ):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as directory:
                run_dir = Path(directory)
                evidence, rows = build_lifecycle(run_dir, "drone-1")
                final = evidence["finalActuatorState"]
                audit = [evidence["initialActuatorState"], final]
                if mutation == "signature":
                    final["request"]["signature"] = "0" * 128
                elif mutation == "run":
                    final["runId"] = "another-run"
                elif mutation == "vehicle":
                    final["vehicleId"] = "drone-2"
                elif mutation == "boolean":
                    final["armed"] = 0
                elif mutation == "timestamp":
                    evidence["capturedAtUnixMs"] += 6000
                elif mutation == "duplicate":
                    audit.insert(1, audit[0])
                elif mutation == "order":
                    audit.reverse()
                elif mutation == "key":
                    evidence["authorityPublicKeyHex"] = "1" * 64
                elif mutation == "unsigned":
                    final["request"].pop("signature")
                (run_dir / "applier-neutral/actuator-lifecycle.jsonl").write_text(
                    "".join(json.dumps(row) + "\n" for row in audit)
                )
                latest = run_dir / "applier-neutral/drone-1.armed-state.json"
                latest.write_text(json.dumps(final if mutation != "latest" else {}))
                if mutation == "missing":
                    latest.unlink()
                result = disarmed_status(run_dir, "drone-1", evidence, rows)
            self.assertEqual(result["status"], "unavailable")
            self.assertIsNone(result["disarmed"])

    def test_disarm_requires_complete_stop_and_no_later_actuator_command(self) -> None:
        for mutation in ("missing-drive", "wrong-sequence", "late-command"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as directory:
                run_dir = Path(directory)
                evidence, rows = build_lifecycle(run_dir, "drone-1")
                if mutation == "missing-drive":
                    rows.pop()
                elif mutation == "wrong-sequence":
                    rows[0]["sequence"] = 99
                else:
                    rows.append(
                        {
                            "runId": run_dir.name,
                            "vehicleId": "drone-1",
                            "stage": "authorized-command",
                            "recordedAtUnixMs": 11_001,
                        }
                    )
                result = disarmed_status(run_dir, "drone-1", evidence, rows)
            self.assertEqual(result["status"], "unavailable")

    def test_unbound_boolean_never_establishes_disarm(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = disarmed_status(Path(directory), "drone-1", {"finalDisarmed": True}, [])
        self.assertEqual(result["status"], "unavailable")
