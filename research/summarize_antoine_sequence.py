#!/usr/bin/env python3
"""Qualify only run-bound sequence-poisoning and recovery observations."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from build_evidence_manifest import build, write_manifest
from evidence_contracts import read_json_object


def exactly_two_publications(raw: dict) -> bool:
    count = raw.get("publishedMessages")
    return type(count) is int and count == 2


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("run_dir", type=Path)
    args = parser.parse_args()
    run_dir = args.run_dir
    raw = read_json_object(run_dir / "research-sequence-poisoning.json")
    flight = read_json_object(run_dir / "gz-flight-proof-report.json")
    run_id = raw.get("runId")
    run_matches = run_id == run_dir.name and run_id == flight.get("localGrant", {}).get("runId")
    neutral_stop = raw.get("neutralStop")
    neutral_stop = neutral_stop if isinstance(neutral_stop, dict) else {}
    gates = {
        "runIdentityMatches": run_matches,
        "exactlyTwoControlledPublications": exactly_two_publications(raw),
        "highWaterAccepted": raw.get("highWaterAccepted") is True,
        "grantMismatchStopObserved": raw.get("grantMismatchStopObserved") is True,
        "recoverySequenceIncreased": (
            type(raw.get("recoverySequence")) is int
            and type(raw.get("highWaterSequence")) is int
            and raw["recoverySequence"] > raw["highWaterSequence"]
        ),
        "recoveryParserAccepted": raw.get("recoveryParserAccepted") is True,
        "actuatorProgressAfterRecovery": raw.get("actuatorProgressAfterRecovery") is True,
        "eventOrderValid": (
            type(raw.get("highWaterPublishAtUnixMs")) is int
            and type(raw.get("highWaterAcceptedAtUnixMs")) is int
            and type(neutral_stop.get("recordedAtUnixMs")) is int
            and type(raw.get("recoveryPublishAtUnixMs")) is int
            and type(raw.get("recoveryAcceptedAtUnixMs")) is int
            and type(raw.get("actuatorProgressAtUnixMs")) is int
            and raw["highWaterPublishAtUnixMs"]
            <= raw["highWaterAcceptedAtUnixMs"]
            <= neutral_stop["recordedAtUnixMs"]
            <= raw["recoveryPublishAtUnixMs"]
            <= raw["recoveryAcceptedAtUnixMs"]
            <= raw["actuatorProgressAtUnixMs"]
        ),
        "independentPoseTimelinePassed": flight.get("poseTimelinePassed") is True,
        "provenanceStable": False,
    }
    provenance = read_json_object(run_dir / "research-provenance-comparison.json")
    gates["provenanceStable"] = (
        provenance.get("runId") == run_id
        and provenance.get("stable") is True
        and provenance.get("profileMatches") is True
    )
    qualified = all(gates.values())
    report = {
        "contract": "damn-vulnerable-drone.antoine-sequence-poisoning-summary",
        "version": "1.0.0",
        "scenario": "setpoint-sequence-poisoning-antoine",
        "runId": run_id,
        "qualification": "bounded_sequence_recovery_observed" if qualified else "inconclusive",
        "gates": gates,
        "highWaterSequence": raw.get("highWaterSequence"),
        "recoverySequence": raw.get("recoverySequence"),
        "recoveryToActuatorProgressMs": raw.get("recoveryToActuatorProgressMs"),
        "horizontalTravelM": flight.get("measured", {}).get("maxHorizontalTravelM"),
        "allLegsReached": flight.get("measured", {}).get("allLegsReached"),
        "limitations": raw.get("limitations", []),
    }
    (run_dir / "research-sequence-poisoning-summary.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    manifest = build(run_dir)
    write_manifest(run_dir, manifest)
    print(json.dumps(report, indent=2))
    return 0 if qualified else 2


if __name__ == "__main__":
    raise SystemExit(main())
