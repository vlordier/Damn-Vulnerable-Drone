#!/usr/bin/env python3
"""Initialize a local Ed25519 key or sign/verify one evidence manifest."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from dvd_research.evidence_contract import (
    initialize_signing_key,
    sign_manifest,
    verify_manifest,
    verify_signature,
)

DEFAULT_KEY = Path.home() / ".config" / "damn-vulnerable-drone" / "manifest-signing.key"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    init_parser = subparsers.add_parser("init-key", help="create a local owner-only signing key")
    init_parser.add_argument("--key", type=Path, default=DEFAULT_KEY)
    init_parser.add_argument("--public-key", type=Path)
    sign_parser = subparsers.add_parser("sign", help="sign a completed manifest")
    sign_parser.add_argument("manifest", type=Path)
    sign_parser.add_argument("--key", type=Path)
    verify_parser = subparsers.add_parser(
        "verify", help="verify manifest digests and signature separately"
    )
    verify_parser.add_argument("manifest", type=Path)
    verify_parser.add_argument("--trusted-public-key", type=Path)
    args = parser.parse_args()

    if args.command == "init-key":
        public_path = args.public_key or Path(str(args.key.expanduser()) + ".pub")
        if not public_path.is_absolute():
            parser.error("public key path must be absolute")
        if public_path.exists() or public_path.is_symlink():
            parser.error("refusing to replace an existing or linked public key")
        public_key = initialize_signing_key(args.key)
        public_path.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
        descriptor = os.open(public_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        with os.fdopen(descriptor, "w", encoding="ascii") as stream:
            stream.write(public_key + "\n")
        print(
            json.dumps(
                {
                    "keyPath": str(args.key.expanduser()),
                    "publicKeyPath": str(public_path),
                    "publicKeyHex": public_key,
                },
                indent=2,
            )
        )
        return 0

    if args.command == "sign":
        key_path = args.key or (
            Path(os.environ["DVD_RESEARCH_SIGNING_KEY"])
            if os.environ.get("DVD_RESEARCH_SIGNING_KEY")
            else None
        )
        if key_path is None:
            parser.error("provide --key or set DVD_RESEARCH_SIGNING_KEY")
        sidecar = sign_manifest(args.manifest, key_path)
        print(sidecar)
        return 0

    integrity_ok, integrity_errors = verify_manifest(args.manifest)
    signature_status, signature_errors = verify_signature(args.manifest, args.trusted_public_key)
    print(
        json.dumps(
            {
                "integrityCheck": "passed" if integrity_ok else "failed",
                "integrityErrors": integrity_errors,
                "signatureStatus": signature_status,
                "signatureErrors": signature_errors,
            },
            indent=2,
        )
    )
    return (
        0
        if integrity_ok
        and signature_status in {"valid_trusted_signature", "valid_unpinned_signature", "unsigned"}
        else 2
    )


if __name__ == "__main__":
    raise SystemExit(main())
