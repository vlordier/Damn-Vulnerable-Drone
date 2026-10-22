"""Verify explicit signed actuator lifecycle evidence against independent audit rows."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from dvd_research.evidence_contract import contains_symlink_component


def _integer(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def disarmed_status(
    run_dir: Path,
    callsign: str,
    evidence: dict[str, Any] | None,
    actuator_records: list[dict[str, Any]],
) -> dict[str, Any]:
    def unavailable(reason: str) -> dict[str, Any]:
        return {"status": "unavailable", "disarmed": None, "reason": reason}

    run_dir = Path(run_dir)
    if run_dir.is_symlink():
        return unavailable("Run directory is a symlink.")
    try:
        run_dir = run_dir.resolve(strict=True)
    except OSError:
        return unavailable("Run directory is missing or cannot be resolved.")
    if not run_dir.is_dir():
        return unavailable("Run path is not a directory.")
    if not isinstance(evidence, dict) or not isinstance(evidence.get("finalActuatorState"), dict):
        return unavailable(
            "No explicit actuator-lifecycle acknowledgement; neutral motors and revoked grants do not establish disarm."
        )
    if evidence.get("runId") != run_dir.name or evidence.get("callsign") != callsign:
        return unavailable("Authority evidence has a mismatched run or vehicle identity.")
    if not isinstance(evidence.get("worldFrameId"), str) or not evidence["worldFrameId"]:
        return unavailable("Authority evidence has no world-frame binding.")
    if any(not isinstance(row, dict) for row in actuator_records):
        return unavailable("Malformed actuator audit rows.")
    try:
        from nacl.exceptions import BadSignatureError
        from nacl.signing import VerifyKey
    except ImportError:
        return unavailable("PyNaCl is required; run the evaluator with uv run --project research.")
    directory = run_dir / "applier-neutral"
    audit_path = directory / "actuator-lifecycle.jsonl"
    latest_path = directory / f"{callsign}.armed-state.json"
    try:
        for path in (audit_path, latest_path):
            relative = path.relative_to(run_dir)
            if contains_symlink_component(run_dir, relative) or path.stat().st_size > 1_048_576:
                return unavailable("Unsafe or oversized actuator-lifecycle artifact.")
        rows = [json.loads(line) for line in audit_path.read_text().splitlines() if line.strip()]
        if any(not isinstance(row, dict) for row in rows):
            return unavailable("Malformed actuator-lifecycle audit.")
        rows = [row for row in rows if row.get("vehicleId") == callsign]
        latest = json.loads(latest_path.read_text())
        final = evidence["finalActuatorState"]
        initial = evidence.get("initialActuatorState")
        requests = evidence.get("actuatorLifecycleRequests")
        captured = evidence.get("capturedAtUnixMs")
        if not rows or latest != final or rows[-1] != final or not isinstance(requests, list):
            return unavailable(
                "Final state does not match the independent lifecycle audit and latest-state file."
            )
        public_key = evidence.get("authorityPublicKeyHex")
        if not isinstance(public_key, str) or len(public_key) != 64:
            return unavailable("Authority verification key is unavailable.")
        key = VerifyKey(bytes.fromhex(public_key))
        last_sequence, last_time = 0, 0
        for row in rows:
            request = row.get("request")
            sequence, timestamp = row.get("sequence"), row.get("recordedAtUnixMs")
            if (
                row.get("contract") != "furia.gzsitl.actuator-lifecycle-state"
                or row.get("version") != "1.0.0"
                or row.get("source") != "gz-sitl-applier"
                or row.get("runId") != run_dir.name
                or row.get("vehicleId") != callsign
                or not _integer(sequence)
                or sequence <= last_sequence
                or not _integer(timestamp)
                or timestamp < last_time
                or not isinstance(row.get("armed"), bool)
                or not isinstance(request, dict)
                or request not in requests
            ):
                return unavailable(
                    "Lifecycle identity, sequence, timestamp or request binding is invalid."
                )
            issued, expires = request.get("issuedAtMs"), request.get("expiresAtMs")
            if (
                request.get("schema") != "furia.gzsitl.actuator-lifecycle"
                or request.get("version") != "1.0.0"
                or request.get("identity") != callsign
                or request.get("runId") != run_dir.name
                or request.get("frameId") != evidence.get("worldFrameId")
                or row.get("frameId") != request.get("frameId")
                or request.get("sequence") != sequence
                or not _integer(request.get("sequence"))
                or request.get("requestId") != f"{callsign}:lifecycle:{sequence}"
                or row.get("requestId") != request.get("requestId")
                or not isinstance(request.get("armed"), bool)
                or request["armed"] != row["armed"]
                or not _integer(request.get("stopCommandSequence"))
                or (request.get("armed") is True and request.get("stopCommandSequence") != 0)
                or row.get("stopCommandSequence") != request["stopCommandSequence"]
                or request.get("signatureAlgorithm") != "Ed25519"
                or not _integer(issued)
                or not _integer(expires)
                or not 0 < expires - issued <= 250
                or not issued - 50 <= timestamp < expires
            ):
                return unavailable("Lifecycle request is malformed, stale or mismatched.")
            signature = request.get("signature")
            if not isinstance(signature, str) or len(signature) != 128:
                return unavailable("Lifecycle request signature is missing or malformed.")
            unsigned = {name: value for name, value in request.items() if name != "signature"}
            key.verify(
                json.dumps(
                    unsigned, sort_keys=True, separators=(",", ":"), ensure_ascii=False
                ).encode(),
                bytes.fromhex(signature),
            )
            last_sequence, last_time = sequence, timestamp
        if (
            not isinstance(initial, dict)
            or initial not in rows
            or initial.get("armed") is not True
            or initial["sequence"] >= final["sequence"]
            or final.get("armed") is not False
            or not _integer(captured)
            or not 0 <= captured - last_time <= 5_000
        ):
            return unavailable("Explicit fresh arm-to-disarm lifecycle is incomplete.")
        stop_sequence = final.get("stopCommandSequence")
        stops = [
            row
            for row in actuator_records
            if row.get("runId") == run_dir.name
            and row.get("vehicleId") == callsign
            and row.get("stage") == "explicit-stop"
            and row.get("authority") == "safe-stop"
            and row.get("neutral") is True
            and row.get("sequence") == stop_sequence
            and _integer(row.get("recordedAtUnixMs"))
            and 0 <= last_time - row["recordedAtUnixMs"] <= 15_000
        ]
        if (
            stop_sequence == 0
            or len(stops) != 2
            or {row.get("actuatorKind") for row in stops} != {"motors", "drive"}
        ):
            return unavailable(
                "Disarm is not bound to independent explicit-stop motor and drive rows."
            )
        stop_time = min(row["recordedAtUnixMs"] for row in stops)
        if any(
            row.get("stage") == "authorized-command"
            and row.get("runId") == run_dir.name
            and row.get("vehicleId") == callsign
            and (not _integer(row.get("recordedAtUnixMs")) or not _integer(row.get("sequence")))
            for row in actuator_records
        ):
            return unavailable("Malformed authorized-command sequence or timestamp.")
        if any(
            row.get("stage") == "authorized-command"
            and row.get("runId") == run_dir.name
            and row.get("vehicleId") == callsign
            and _integer(row.get("recordedAtUnixMs"))
            and (row["recordedAtUnixMs"] > stop_time or row.get("sequence", 0) > stop_sequence)
            for row in actuator_records
        ):
            return unavailable("An actuator command follows the stop bound to disarm.")
        return {
            "status": "measured",
            "disarmed": True,
            "source": "gz-sitl-applier",
            "sequence": last_sequence,
            "recordedAtUnixMs": last_time,
            "stopCommandSequence": stop_sequence,
            "signatureVerified": True,
            "transitionCount": len(rows),
        }
    except (OSError, ValueError, TypeError, KeyError, BadSignatureError) as error:
        return unavailable(f"Invalid actuator lifecycle evidence: {type(error).__name__}.")
