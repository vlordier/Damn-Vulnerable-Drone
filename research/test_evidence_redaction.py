from __future__ import annotations

import unittest

from antoine_nats_observation import sanitize
from build_evidence_manifest import PUBLIC_EVIDENCE
from export_research_csv import FIELDS


class EvidenceRedactionTests(unittest.TestCase):
    def test_manifest_allowlist_excludes_secrets_and_runtime_control_files(self) -> None:
        forbidden_parts = {
            ".env",
            "private-key",
            "signing-key",
            "signing-seed",
            "nats-server.conf",
            "interactive-profile-receipt.json",
            ".research-provenance-expected.env",
            "process-supervisor",
            "research-profile.env",
        }
        self.assertFalse(
            [
                path
                for path in PUBLIC_EVIDENCE
                if any(part in path.lower() for part in forbidden_parts)
            ]
        )

    def test_csv_schema_is_explicit_and_excludes_host_and_secret_fields(self) -> None:
        forbidden = {"hostPath", "runtimeControlFile", "signingKey", "privateKey", "secret"}
        self.assertFalse(forbidden.intersection(FIELDS))
        self.assertEqual(len(FIELDS), len(set(FIELDS)))

    def test_nats_observer_redacts_nested_secret_fields(self) -> None:
        raw = {
            "runId": "run-a",
            "executionGrant": {"signature": "secret-signature", "targetEnuMm": [1, 2, 3]},
            "signing_seed": "private-seed",
            "credentialBlob": "credential",
        }
        safe = sanitize(raw)
        self.assertEqual(safe["executionGrant"]["signature"], "[redacted]")
        self.assertEqual(safe["signing_seed"], "[redacted]")
        self.assertEqual(safe["credentialBlob"], "[redacted]")
        self.assertEqual(safe["executionGrant"]["targetEnuMm"], [1, 2, 3])


if __name__ == "__main__":
    unittest.main()
