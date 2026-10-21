"""Signed lifecycle fixtures shared by evidence and full-report tests."""

import json
from pathlib import Path

from nacl.signing import SigningKey


def build_lifecycle(run_dir: Path, callsign: str, model: str = "isr") -> tuple[dict, list[dict]]:
    key = SigningKey(bytes([42]) * 32)
    evidence = {
        "runId": run_dir.name,
        "callsign": callsign,
        "worldFrameId": "enu-test",
        "authorityPublicKeyHex": key.verify_key.encode().hex(),
        "capturedAtUnixMs": 11_030,
    }
    rows, requests = [], []
    for sequence, armed, issued, stamp, stop in (
        (1, True, 1000, 1010, 0),
        (100, False, 11_010, 11_020, 100),
    ):
        request = {
            "schema": "furia.gzsitl.actuator-lifecycle",
            "version": "1.0.0",
            "identity": callsign,
            "runId": run_dir.name,
            "frameId": "enu-test",
            "sequence": sequence,
            "requestId": f"{callsign}:lifecycle:{sequence}",
            "armed": armed,
            "stopCommandSequence": stop,
            "issuedAtMs": issued,
            "expiresAtMs": issued + 250,
            "signatureAlgorithm": "Ed25519",
        }
        request["signature"] = key.sign(
            json.dumps(request, sort_keys=True, separators=(",", ":")).encode()
        ).signature.hex()
        requests.append(request)
        rows.append(
            {
                "contract": "furia.gzsitl.actuator-lifecycle-state",
                "version": "1.0.0",
                "source": "gz-sitl-applier",
                "vehicleId": callsign,
                "modelId": model,
                "runId": run_dir.name,
                "frameId": "enu-test",
                "sequence": sequence,
                "armed": armed,
                "recordedAtUnixMs": stamp,
                "requestId": request["requestId"],
                "stopCommandSequence": stop,
                "request": request,
            }
        )
    evidence.update(
        {
            "initialActuatorState": rows[0],
            "finalActuatorState": rows[1],
            "actuatorLifecycleRequests": requests,
        }
    )
    directory = run_dir / "applier-neutral"
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "actuator-lifecycle.jsonl").open("a") as stream:
        for row in rows:
            stream.write(json.dumps(row) + "\n")
    (directory / f"{callsign}.armed-state.json").write_text(json.dumps(rows[-1]))
    actuator_rows = [
        {
            "runId": run_dir.name,
            "vehicleId": callsign,
            "stage": "explicit-stop",
            "authority": "safe-stop",
            "neutral": True,
            "sequence": 100,
            "actuatorKind": kind,
            "recordedAtUnixMs": 11_000,
        }
        for kind in ("motors", "drive")
    ]
    with (directory / "actuator-output.jsonl").open("a") as stream:
        for row in actuator_rows:
            stream.write(json.dumps(row) + "\n")
    return evidence, actuator_rows
