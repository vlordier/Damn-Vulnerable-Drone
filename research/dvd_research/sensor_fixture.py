"""Run-local fixture signer for the FURIA GPS envelope contract v1."""

from __future__ import annotations

import hashlib
import json
import os
from typing import Any

from nacl.signing import SigningKey

DOMAIN = b"FURIA-GZ-SENSOR-SIGNATURE-V1\n"
SUBJECT = "brenta.sensor.gps.drone-1"


def canonical_json(value: Any) -> bytes:
    """Match serde_json's recursively sorted compact JSON for sensor values."""
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def fixture_signer() -> tuple[SigningKey, str]:
    """Load only the dedicated test seed inherited by the run-owned fixture."""
    seed_hex = os.environ.get("FURIA_GZ_SENSOR_SIGNING_SEED_HEX", "")
    if len(seed_hex) != 64 or any(c not in "0123456789abcdef" for c in seed_hex):
        raise ValueError("fixture requires a 32-byte lowercase test seed in the run environment")
    signer = SigningKey(bytes.fromhex(seed_hex))
    public_key = bytes(signer.verify_key)
    key_id = hashlib.sha256(public_key).digest()[:16].hex()
    configured = os.environ.get("FURIA_SENSOR_VERIFYING_KEYS_JSON", "")
    try:
        registry = json.loads(configured)
    except json.JSONDecodeError as error:
        raise ValueError("fixture requires the run-local verifying-key registry") from error
    if not isinstance(registry, dict) or registry.get(key_id) != public_key.hex():
        raise ValueError("fixture signer is not pinned by this run's verifying-key registry")
    return signer, key_id


def sign_envelope(envelope: dict[str, Any], subject: str = SUBJECT) -> dict[str, Any]:
    if subject != SUBJECT:
        raise ValueError("fixture signer is restricted to the run's drone-1 GPS subject")
    signer, key_id = fixture_signer()
    body = dict(envelope)
    body.pop("authentication", None)
    signed = DOMAIN + subject.encode("ascii") + b"\n" + canonical_json(body)
    signature = signer.sign(signed).signature.hex()
    body["authentication"] = {
        "contract": "furia.sensor.signature.v1",
        "algorithm": "Ed25519",
        "key_id": key_id,
        "signature_hex": signature,
    }
    return body


def build_case(envelope: dict[str, Any], case: str, now_ms: int) -> dict[str, Any]:
    """Create one deterministic timestamp/sequence permutation and sign it."""
    value = dict(envelope)
    value.pop("authentication", None)
    if value.get("schema") != "brenta.sensor.gps.v1" or value.get("identity") != "drone-1":
        raise ValueError("fixture input must be one captured drone-1 GPS envelope")
    if value.get("frame_id") != "isr::base_link::gps":
        raise ValueError("fixture input GPS frame does not match the run contract")
    sequence = value.get("sequence")
    if type(sequence) is not int or sequence < 1:
        raise ValueError("fixture input must include a positive integer sequence")

    if case in {"fresh-sample", "duplicate-sequence"}:
        value["timestamp_ms"] = now_ms
        value["expires_at_ms"] = now_ms + 1_000
    elif case == "stale-sample":
        value["timestamp_ms"] = now_ms - 500
        # Keep the envelope live at ingress so this isolates sample age for
        # the downstream navigation freshness check.
        value["expires_at_ms"] = now_ms + 400
    elif case == "future-sample":
        value["timestamp_ms"] = now_ms + 500
        value["expires_at_ms"] = now_ms + 1_000
    elif case == "out-of-order-sequence":
        if sequence < 2:
            raise ValueError("cannot create an earlier sequence from sequence 1")
        value["sequence"] = sequence - 1
        value["timestamp_ms"] = now_ms
        value["expires_at_ms"] = now_ms + 1_000
    else:
        raise ValueError("unsupported fixture case")
    return sign_envelope(value, SUBJECT)
