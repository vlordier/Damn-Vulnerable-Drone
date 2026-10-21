# Antoine research foundations review

Review date: 2026-10-19

## Critical findings

1. **Qualification logic has multiple owners (P0).** The web console and CSV now consume one classifier, validate manifest digests, and bind manifest run IDs to their evidence directories. Flight-baseline qualification requires an authorized complete profile and measured independent ground-pose dwell; it does not claim a physical safe-stop. Scenario-family parity fixtures are present in `test_evidence_classifier_parity.py`; rendered-page review still depends on a browser runtime.
2. **Provenance scope was too broad (P1).** Pre/post snapshots are present, but the full dirty-state hash also treated documentation changes as source changes. Keep that digest as audit context and compare code/build/configuration fingerprints plus exact runtime inputs for qualification. `dvd-fgt.9.4` narrows this gate; source hashes still do not prove which binary the operating system loaded.
3. **Integrity is not authenticity (P1).** An unsigned manifest can be rewritten together with its hashes. Verification detects accidental evidence changes only when the manifest itself is trusted. Local signing and signature-state reporting are tracked in `dvd-fgt.9.3`.
4. **Process ownership has PID-reuse windows (P1).** Cleanup records PID/start-time/process-group identity and rechecks before signaling. Linux uses `pidfd_open` plus `pidfd_send_signal` after a post-open identity check, preventing a reused PID from receiving the signal. The GPS forwarder now receives a process-only signal instead of a process-group kill. macOS still has a narrow non-atomic `ps`-to-`kill` race because Python exposes no pidfd equivalent there; `dvd-fgt.8.1` remains open for that platform-specific gap.
5. **The measured injection may poison sequence state (P1 research question).** The local controller can consume a high sequence before the grant-target mismatch stops actuation. Existing evidence establishes bounded denial in the local profile, not the recovery duration or persistence boundary. A one-message recovery campaign is `dvd-fgt.10` and children.
6. **Scenario activity is only partly sourced (P1).** A JSON catalog now drives Antoine status badges, manifest coverage, and command labels; active runner paths are checked for executable presence. The catalog still lacks machine-readable evidence gates and guide/argument parity checks, tracked by `dvd-fgt.7.1`.

## Current patch

- `scenario-catalog.json` is the status and command source for the Antoine attacks list and evidence manifests.
- The manifest builder rejects an active catalog row whose runner is missing or non-executable. The UI downgrades that row to `unavailable` and logs the catalog defect.
- The research console's command list now comes from the same catalog; there is no second hard-coded runner list.

These checks are static/local. Bounded GPS signer campaigns also produced ingress-only Gazebo observations; see `findings.md` for run IDs and qualification limits. Existing evidence still has the limitations recorded in `ARCHIVE-REQUALIFICATION.md` and `antoine-scenario-adaptation.md`.
