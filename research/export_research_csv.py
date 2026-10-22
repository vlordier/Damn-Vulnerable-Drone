#!/usr/bin/env python3
"""Export comparable, redacted measurements from Antoine run manifests."""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from pathlib import Path

from dvd_research.evidence_contract import classify_measurements, verify_manifest, verify_signature

FIELDS = [
    "evidenceIntegrity",
    "manifestAuthentication",
    "provenanceStable",
    "runStatus",
    "runIdentityMatches",
    "observationBoundsValid",
    "cameraObservationBoundsValid",
    "runId",
    "scenario",
    "physicsEngine",
    "publishedMessages",
    "capturedMessages",
    "parserAcceptedMutation",
    "antiReplayRejectionObserved",
    "replayQualified",
    "injectionImpactQualified",
    "gpsTamperingQualified",
    "revokeToNeutralMs",
    "tamperingRejectedObserved",
    "gpsReplayObservationQualified",
    "gpsReplayCase",
    "validFixtureSignature",
    "sensorIngressAccepted",
    "freshnessUseDecisionObserved",
    "acceptedGpsTraceDelta",
    "cameraTopicListed",
    "cameraPublisherPresent",
    "cameraFramesCaptured",
    "cameraDimensions",
    "cameraObservationStatus",
    "neutralOutputObserved",
    "neutralizationPassed",
    "localFlightPassed",
    "allLegsReached",
    "peakAltitudeM",
    "horizontalTravelM",
    "publishToStopMs",
    "baselineEligible",
    "physicalSafeStopPassed",
    "groundSettlePassed",
    "settledAtGround",
    "groundSettleDwellSecondsObserved",
    "sequenceRecoveryObservationQualified",
    "highWaterSequence",
    "recoverySequence",
    "recoveryToActuatorProgressMs",
    "actuatorCommands",
    "neutralRows",
    "baselineComparable",
    "baselineHorizontalTravelM",
    "horizontalTravelDeltaM",
    "baselinePeakAltitudeM",
    "peakAltitudeDeltaM",
    "swarmFlightStatus",
    "baselineQualified",
    "gazeboAttitudeQualified",
]


def verify_evidence_manifest(manifest_path: Path, manifest: dict) -> bool:
    if manifest.get("contract") != "damn-vulnerable-drone.antoine-evidence-manifest":
        return False
    evidence = manifest.get("publicEvidence")
    if not isinstance(evidence, list) or not evidence:
        return False
    valid, _ = verify_manifest(manifest_path)
    return valid


def rows(root: Path):
    for path in sorted(root.glob("*/research-evidence-manifest.json")):
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(manifest, dict) or not verify_evidence_manifest(path, manifest):
            print(f"Skipping unverified evidence manifest: {path}", file=sys.stderr)
            continue
        if manifest.get("runId") != path.parent.name:
            print(f"Skipping manifest with mismatched run identity: {path}", file=sys.stderr)
            continue
        measurements = manifest.get("measurements", {})
        trusted_key = (
            Path(os.environ["DVD_RESEARCH_TRUSTED_PUBLIC_KEY"])
            if os.environ.get("DVD_RESEARCH_TRUSTED_PUBLIC_KEY")
            else None
        )
        signature_status, _ = verify_signature(path, trusted_key)
        row = {field: "" for field in FIELDS}
        row.update(
            {
                "evidenceIntegrity": "verified against manifest digests",
                "manifestAuthentication": signature_status,
                "runId": manifest.get("runId", path.parent.name),
                "scenario": manifest.get("scenario", ""),
                "physicsEngine": manifest.get("physicsEngine", ""),
            }
        )
        measurements = measurements if isinstance(measurements, dict) else {}
        row["runStatus"] = (
            "inconclusive"
            if signature_status
            in {
                "invalid_signature",
                "manifest_digest_mismatch",
                "verification_unavailable",
                "trusted_key_unavailable",
                "untrusted_signer",
            }
            else classify_measurements(measurements)
        )
        row.update(
            {field: measurements.get(field, "") for field in FIELDS if field in measurements}
        )
        dimensions = measurements.get("cameraDimensions")
        if isinstance(dimensions, (list, dict)):
            row["cameraDimensions"] = json.dumps(dimensions, sort_keys=True, separators=(",", ":"))
        scenario = manifest.get("scenario")
        if scenario == "gazebo-camera-readable-topic-antoine" or any(
            measurements.get(field) is not None
            for field in ("cameraTopicListed", "cameraPublisherPresent", "cameraFramesCaptured")
        ):
            listed = measurements.get("cameraTopicListed")
            publisher = measurements.get("cameraPublisherPresent")
            frames = measurements.get("cameraFramesCaptured")
            if frames:
                row["cameraObservationStatus"] = "frames_captured"
            elif listed is False:
                row["cameraObservationStatus"] = "topic_not_listed_in_probe_partition"
            elif listed is True and publisher is False:
                row["cameraObservationStatus"] = "topic_listed_without_publisher"
            elif listed is True and publisher is True:
                row["cameraObservationStatus"] = "publisher_present_without_frame"
            elif frames == 0:
                row["cameraObservationStatus"] = "no_frame_observed_status_unknown"
            else:
                row["cameraObservationStatus"] = "not_recorded"
        elif scenario == "gps-signature-tampering-antoine":
            row["cameraObservationStatus"] = "separate_camera_rehearsal"
        yield row


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path("build_logs/research/antoine-scenarios"))
    parser.add_argument(
        "--output", type=Path, default=Path("build_logs/research/antoine-research-summary.csv")
    )
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", newline="", encoding="utf-8") as output:
        writer = csv.DictWriter(output, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows(args.root))
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
