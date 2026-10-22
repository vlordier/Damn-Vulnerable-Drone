"""Shared evidence integrity and qualification rules for the research UI/export."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from pathlib import Path

MANIFEST_CONTRACT = "damn-vulnerable-drone.antoine-evidence-manifest"
SIGNATURE_CONTRACT = "damn-vulnerable-drone.antoine-evidence-signature"
SIGNATURE_FILENAME = "research-evidence-signature.json"
DIGEST = re.compile(r"^[0-9a-f]{64}$")


def contains_symlink_component(root: Path, relative_path: Path) -> bool:
    current = root
    for component in relative_path.parts:
        current = current / component
        if current.is_symlink():
            return True
    return False


def verify_manifest(manifest_path: Path) -> tuple[bool, list[str]]:
    errors: list[str] = []
    if manifest_path.is_symlink() or manifest_path.parent.is_symlink():
        return False, ["manifest or run directory is a symlink"]
    try:
        import json

        run_dir = manifest_path.parent.resolve(strict=True)
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return False, [f"cannot read manifest: {type(exc).__name__}"]
    if not isinstance(manifest, dict) or manifest.get("contract") != MANIFEST_CONTRACT:
        return False, ["manifest contract is missing or unsupported"]
    if not isinstance(manifest.get("runId"), str) or manifest["runId"] != manifest_path.parent.name:
        errors.append("manifest runId does not match its directory")
    items = manifest.get("publicEvidence")
    if not isinstance(items, list) or not items:
        return False, errors + ["publicEvidence must be a non-empty list"]
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            errors.append("evidence entry is not an object")
            continue
        name, digest, size = item.get("name"), item.get("sha256"), item.get("sizeBytes")
        if (
            not isinstance(name, str)
            or not name
            or Path(name).is_absolute()
            or ".." in Path(name).parts
            or name in seen
        ):
            errors.append("evidence path is invalid or duplicated")
            continue
        seen.add(name)
        if (
            not isinstance(digest, str)
            or not DIGEST.fullmatch(digest)
            or type(size) is not int
            or size < 0
        ):
            errors.append(f"evidence metadata is invalid: {name}")
            continue
        path = manifest_path.parent / name
        try:
            if contains_symlink_component(run_dir, Path(name)) or not path.is_file():
                errors.append(f"evidence file is missing or escapes run directory: {name}")
                continue
            try:
                path.resolve(strict=True).relative_to(run_dir)
            except (OSError, ValueError):
                errors.append(f"evidence file is missing or escapes run directory: {name}")
                continue
            if path.stat().st_size != size:
                errors.append(f"evidence size mismatch: {name}")
                continue
            digest_builder = hashlib.sha256()
            with path.open("rb") as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b""):
                    digest_builder.update(block)
            if digest_builder.hexdigest() != digest:
                errors.append(f"evidence digest mismatch: {name}")
        except OSError:
            errors.append(f"cannot read evidence file: {name}")
    return not errors, errors


def _manifest_bytes(manifest_path: Path) -> bytes:
    return manifest_path.read_bytes()


def _signature_message(run_id: str, digest: str) -> bytes:
    return b"DVD-ANTOINE-MANIFEST-V1\0" + run_id.encode("utf-8") + b"\0" + bytes.fromhex(digest)


def initialize_signing_key(key_path: Path) -> str:
    """Create a new private key with owner-only permissions and return public hex."""
    try:
        from nacl.signing import SigningKey
    except ImportError as error:
        raise RuntimeError("PyNaCl is required for manifest signing") from error
    key_path = key_path.expanduser()
    if not key_path.is_absolute():
        raise ValueError("signing key path must be absolute")
    key_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if key_path.parent.is_symlink() or key_path.exists() or key_path.is_symlink():
        raise FileExistsError("refusing to replace an existing or linked signing key")
    key = SigningKey.generate()
    descriptor = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(bytes(key))
        stream.flush()
        os.fsync(stream.fileno())
    os.chmod(key_path, 0o600)
    return bytes(key.verify_key).hex()


def _load_signing_key(key_path: Path):
    try:
        from nacl.signing import SigningKey
    except ImportError as error:
        raise RuntimeError("PyNaCl is required for manifest signing") from error
    key_path = key_path.expanduser()
    if key_path.is_symlink() or not key_path.is_file():
        raise ValueError("signing key is missing or linked")
    if key_path.stat().st_mode & 0o077:
        raise ValueError("signing key permissions must be owner-only")
    secret = key_path.read_bytes()
    if len(secret) != 32:
        raise ValueError("signing key must contain exactly 32 raw bytes")
    return SigningKey(secret)


def sign_manifest(manifest_path: Path, key_path: Path) -> Path:
    manifest_path = Path(manifest_path)
    integrity_ok, integrity_errors = verify_manifest(manifest_path)
    if not integrity_ok:
        raise ValueError(f"refusing to sign an invalid manifest: {integrity_errors}")
    manifest = json.loads(_manifest_bytes(manifest_path))
    if not isinstance(manifest, dict) or manifest.get("contract") != MANIFEST_CONTRACT:
        raise ValueError("cannot sign an unsupported evidence manifest")
    key = _load_signing_key(key_path)
    manifest_digest = hashlib.sha256(_manifest_bytes(manifest_path)).hexdigest()
    run_id = manifest.get("runId")
    if not isinstance(run_id, str) or run_id != manifest_path.parent.name:
        raise ValueError("manifest run ID does not match its directory")
    message = _signature_message(run_id, manifest_digest)
    signature = key.sign(message).signature
    signature_record = {
        "contract": SIGNATURE_CONTRACT,
        "version": "1.0.0",
        "runId": run_id,
        "manifestSha256": manifest_digest,
        "publicKeyHex": bytes(key.verify_key).hex(),
        "signatureHex": signature.hex(),
        "algorithm": "Ed25519",
    }
    destination = manifest_path.parent / SIGNATURE_FILENAME
    payload = json.dumps(signature_record, indent=2, sort_keys=True) + "\n"
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=destination.parent,
        prefix=".research-signature-",
        suffix=".tmp",
        delete=False,
    ) as stream:
        temporary_path = Path(stream.name)
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary_path, destination)
    os.chmod(destination, 0o644)
    return destination


def verify_signature(
    manifest_path: Path,
    trusted_public_key_path: Path | None = None,
) -> tuple[str, list[str]]:
    """Verify signature sidecar and optional pinned signer key.

    Statuses distinguish unsigned/malformed signatures from manifest digest
    failures and from valid signatures made by an unpinned key.
    """
    signature_path = Path(manifest_path).parent / SIGNATURE_FILENAME
    manifest_path = Path(manifest_path)
    if (
        manifest_path.is_symlink()
        or manifest_path.parent.is_symlink()
        or signature_path.is_symlink()
    ):
        return "invalid_signature", ["signature sidecar is linked"]
    if signature_path.exists() and (
        not signature_path.is_file() or signature_path.stat().st_size > 16_384
    ):
        return "invalid_signature", [
            "signature sidecar is not a regular file or exceeds the size limit"
        ]
    try:
        sidecar = json.loads(signature_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return "unsigned", ["signature sidecar is missing"]
    except (OSError, json.JSONDecodeError):
        return "invalid_signature", ["signature sidecar cannot be read"]
    if not isinstance(sidecar, dict) or sidecar.get("contract") != SIGNATURE_CONTRACT:
        return "invalid_signature", ["signature sidecar contract is invalid"]
    try:
        manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
        actual_digest = hashlib.sha256(_manifest_bytes(Path(manifest_path))).hexdigest()
        public_key = bytes.fromhex(sidecar.get("publicKeyHex", ""))
        signature = bytes.fromhex(sidecar.get("signatureHex", ""))
        if len(public_key) != 32 or len(signature) != 64:
            raise ValueError("invalid key or signature length")
        from nacl.signing import VerifyKey

        if sidecar.get("manifestSha256") != actual_digest:
            return "manifest_digest_mismatch", ["manifest digest differs from signed digest"]
        if (
            not isinstance(manifest, dict)
            or sidecar.get("runId") != manifest.get("runId")
            or manifest.get("runId") != Path(manifest_path).parent.name
        ):
            return "invalid_signature", ["signature run identity does not match"]
        VerifyKey(public_key).verify(_signature_message(sidecar["runId"], actual_digest), signature)
    except ImportError:
        return "verification_unavailable", ["PyNaCl is not installed"]
    except Exception as error:
        return "invalid_signature", [f"signature verification failed: {type(error).__name__}"]

    if trusted_public_key_path:
        trusted_path = Path(trusted_public_key_path)
        try:
            trusted_hex = trusted_path.read_text(encoding="ascii").strip()
            trusted = bytes.fromhex(trusted_hex)
        except (OSError, ValueError, UnicodeError):
            return "trusted_key_unavailable", ["trusted public key cannot be read"]
        if len(trusted) != 32 or trusted != public_key:
            return "untrusted_signer", [
                "signature is valid but signer does not match the pinned public key"
            ]
        return "valid_trusted_signature", []
    return "valid_unpinned_signature", [
        "signature is valid, but no trusted public key was configured"
    ]


def sign_manifest_if_configured(manifest_path: Path) -> Path | None:
    key_path = os.environ.get("DVD_RESEARCH_SIGNING_KEY")
    if not key_path:
        return None
    return sign_manifest(manifest_path, Path(key_path))


def classify_measurements(measurements: dict) -> str:
    """Return the sole public qualification label from normalized manifest gates."""
    if not isinstance(measurements, dict):
        return "inconclusive"
    if measurements.get("provenanceStable") is not True:
        return "inconclusive"
    bound = measurements.get("runIdentityMatches") is True
    if bound and measurements.get("swarmFlightStatus") == "failed":
        return "failed_swarm_flight"
    if (
        bound
        and measurements.get("swarmFlightStatus") == "passed"
        and measurements.get("swarmFlightPassed") is True
        and measurements.get("baselineQualified") is True
        and measurements.get("gazeboAttitudeQualified") is True
        and measurements.get("poseStreamQualityPassed") is True
        and measurements.get("allPairwiseSeparationsMeasured") is True
        and measurements.get("allPairwiseSeparationsAtLeast1m") is True
        and measurements.get("behaviorPassed") is True
        and measurements.get("mavlinkSwarmPassed") is not False
    ):
        return "qualified_swarm_flight"
    if (
        bound
        and measurements.get("baselineEligible") is True
        and measurements.get("groundSettlePassed") is True
    ):
        return "qualified_baseline"
    if bound and measurements.get("injectionImpactQualified") is True:
        return "qualified_attack_observation"
    if (
        bound
        and measurements.get("gpsTamperingQualified") is True
        and measurements.get("tamperingRejectedObserved") is True
    ):
        return "qualified_signature_mutation_rejection"
    if bound and measurements.get("gpsReplayObservationQualified") is True:
        return "observation_only"
    if (
        bound
        and measurements.get("replayQualified") is True
        and measurements.get("antiReplayRejectionObserved") is True
    ):
        return "qualified_replay_rejection"
    if bound and measurements.get("revokeQualified") is True:
        return "qualified_local_revoke_stop_observation"
    if bound and measurements.get("sequenceRecoveryObservationQualified") is True:
        return "observation_only"
    if bound and measurements.get("cameraObservationBoundsValid") is True:
        frames = measurements.get("cameraFramesCaptured")
        if type(frames) is int and frames > 0:
            return "observation_only"
    if bound and measurements.get("observationBoundsValid") is True:
        count = measurements.get("capturedMessages")
        if type(count) is int and count > 0:
            return "observation_only"
    return "inconclusive"
