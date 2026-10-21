# Antoine archive requalification (2026-10-19)

Derived summaries were regenerated from surviving run-owned raw evidence under the strict identity/cardinality/timestamp rules. Raw evidence was not edited.

- Baseline runs with a proof `localGrant.runId` matching the directory remain eligible when all independent flight gates pass.
- Replay runs retain qualification only when their raw capture, replay, and rejection timestamps are ordered and the run identity matches.
- GPS tampering artifacts that predate raw `runId` recording are inconclusive, even when their previous summary claimed rejection. A fresh run (`antoine-gps-tampering-20261019T123735Z-33947`) passes all strict gates.
- The early-revoke run `antoine-early-revoke-20261019T122851Z-85743` interrupts the mission and records same-run acceptance then neutral output. The later revoke run `...T122718Z-73409` completed all legs before revoke and is inconclusive as an early interruption.
- Two old directories (`antoine-baseline-20261019T102942Z-91350` and `antoine-gps-tampering-20261019T112431Z-87928`) contain manifests whose referenced evidence is missing or no longer matches the recorded digest. The exporter skips them; their evidence cannot be reconstructed from surviving raw files.

Manifest hashes provide integrity checking against later modification, not producer or binary attestation. Runs also predate any before/after source-state pinning, so their source provenance should not be treated as immutable build provenance.
