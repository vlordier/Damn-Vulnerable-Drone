#!/usr/bin/env python3
"""Attach independent Gazebo proof to a bounded GPS tampering observation."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from build_evidence_manifest import build, write_manifest
from dvd_research.strict_json import loads as strict_json_loads
from evidence_contracts import exact_count, run_identity_matches


def accepted_trace_delta_is(tamper: dict, expected: int) -> bool:
    value = tamper.get("acceptedGpsTraceDelta")
    return type(value) is int and value == expected


def read(path: Path) -> dict:
    try:
        value = strict_json_loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def read_sensor_profile(path: Path) -> dict:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
        boundary = next(
            (index for index, line in enumerate(lines) if line.startswith("GZ_PARTITION=")),
            len(lines),
        )
        value = strict_json_loads("\n".join(lines[:boundary]))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--launcher-exit", type=int, required=True)
    parser.add_argument("--probe-exit", type=int, required=True)
    args = parser.parse_args()
    run_dir = args.run_dir
    tamper = read(run_dir / "research-gps-tampering.json")
    flight = read(run_dir / "gz-flight-proof-report.json")
    sensor_profile = read_sensor_profile(run_dir / "sensor-profile.json")
    flight_grant = flight.get("localGrant")
    flight_grant = flight_grant if isinstance(flight_grant, dict) else {}
    tamper_run_id = tamper.get("runId")
    flight_run_id = flight_grant.get("runId")
    identity_matches = run_identity_matches(run_dir, tamper_run_id, flight_run_id)
    case = tamper.get("case", "signature-mutation")
    measured = flight.get("measured")
    measured = measured if isinstance(measured, dict) else {}
    gates = {
        "launcherExitZero": args.launcher_exit == 0,
        "probeExitZero": args.probe_exit == 0,
        "runIdentityMatches": identity_matches,
        "exactlyOneMessagePublished": exact_count(tamper, "publishedMessages", 1),
        "liveSignedGpsObserved": tamper.get("capturedSignedMessage") is True,
        "independentPoseTimelinePassed": flight.get("poseTimelinePassed") is True,
        "actuatorNeutralizationPassed": flight.get("actuatorNeutralizationPassed") is True,
        "localFlightPassed": flight.get("localFlightPassed") is True,
        "allLegsReached": measured.get("allLegsReached") is True,
        "sensorProfileRecorded": sensor_profile.get("sourceModified") is False,
    }
    if case == "signature-mutation":
        gates["tamperedSignatureNotAccepted"] = tamper.get("tamperingRejectedObserved") is True
    elif case == "future-sample":
        gates["fixtureTimestampExceedsIngressTolerance"] = (
            type(tamper.get("sampleAgeAtPublishMs")) is int
            and tamper["sampleAgeAtPublishMs"] <= -250
        )
        gates["futureSignedSampleNotAcceptedAtIngress"] = tamper.get(
            "validFixtureSignature"
        ) is True and accepted_trace_delta_is(tamper, 0)
    elif case == "stale-sample":
        gates["sampleWasOldButEnvelopeStillLive"] = (
            type(tamper.get("sampleAgeAtPublishMs")) is int
            and 450 <= tamper["sampleAgeAtPublishMs"] <= 750
            and type(tamper.get("sampleExpiresAtUnixMs")) is int
            and type(tamper.get("publishedAtUnixMs")) is int
            and tamper["sampleExpiresAtUnixMs"] > tamper["publishedAtUnixMs"]
        )
        gates["fixtureSignedSampleAcceptedAtIngress"] = tamper.get(
            "validFixtureSignature"
        ) is True and accepted_trace_delta_is(tamper, 1)
    elif case == "duplicate-sequence":
        gates["duplicateSequenceMatchesCapturedSample"] = type(
            tamper.get("capturedSequence")
        ) is int and tamper.get("injectedSequence") == tamper.get("capturedSequence")
        gates["fixtureSignedSampleAcceptedAtIngress"] = tamper.get(
            "validFixtureSignature"
        ) is True and accepted_trace_delta_is(tamper, 1)
    elif case == "out-of-order-sequence":
        gates["injectedSequenceOlderThanLatestCaptured"] = (
            tamper.get("injectedSequenceOlderThanLatestCaptured") is True
        )
        gates["fixtureSignedSampleAcceptedAtIngress"] = tamper.get(
            "validFixtureSignature"
        ) is True and accepted_trace_delta_is(tamper, 1)
    else:
        gates["fixtureSignedSampleAcceptedAtIngress"] = tamper.get(
            "validFixtureSignature"
        ) is True and accepted_trace_delta_is(tamper, 1)
    gps_observation_qualified = all(gates.values()) and case != "signature-mutation"
    result = {
        "contract": "damn-vulnerable-drone.antoine-gps-tampering-summary",
        "version": "1.0.0",
        "scenario": tamper.get("scenario", "gps-signature-tampering-antoine"),
        "runId": run_dir.name,
        "artifactRunIds": {"tampering": tamper_run_id, "flightProof": flight_run_id},
        "qualification": (
            "qualified local signature-mutation rejection only; test-only key and idealized Gazebo GPS"
            if case == "signature-mutation"
            else "qualified sensor-ingress observation only; downstream freshness use is not exposed by current controller traces"
        ),
        "gates": gates,
        "tamperingQualified": case == "signature-mutation" and all(gates.values()),
        "experimentQualified": all(gates.values()),
        "gpsReplayObservationQualified": gps_observation_qualified,
        "case": case,
        "validFixtureSignature": tamper.get("validFixtureSignature") is True,
        "sensorIngressAccepted": tamper.get("sensorIngressAccepted"),
        "freshnessUseDecisionObserved": tamper.get("freshnessUseDecisionObserved") is True,
        "tamperingRejectedObserved": tamper.get("tamperingRejectedObserved"),
        "acceptedGpsTraceDelta": tamper.get("acceptedGpsTraceDelta"),
        "publishedMessages": tamper.get("publishedMessages"),
        "measurements": {
            "localFlightPassed": flight.get("localFlightPassed"),
            "allLegsReached": measured.get("allLegsReached"),
            "actuatorNeutralizationPassed": flight.get("actuatorNeutralizationPassed"),
            "neutralRows": measured.get("applierNeutralRows"),
            "peakAltitudeM": measured.get("peakAltitudeM"),
            "horizontalTravelM": measured.get("maxHorizontalTravelM"),
        },
        "flightProof": {
            "localFlightPassed": flight.get("localFlightPassed"),
            "poseTimelinePassed": flight.get("poseTimelinePassed"),
            "allLegsReached": measured.get("allLegsReached"),
            "maxHorizontalTravelM": measured.get("maxHorizontalTravelM"),
            "peakAltitudeM": measured.get("peakAltitudeM"),
        },
        "sensorProfile": {
            "contract": sensor_profile.get("contract"),
            "sourceSha256": sensor_profile.get("sourceSha256"),
            "overlaySha256": sensor_profile.get("overlaySha256"),
            "sourceModified": sensor_profile.get("sourceModified"),
            "controllerHz": sensor_profile.get("controllerHz"),
            "gazeboPartition": next(
                (
                    line.partition("=")[2]
                    for line in (run_dir / "sensor-profile.json")
                    .read_text(encoding="utf-8")
                    .splitlines()
                    if line.startswith("GZ_PARTITION=")
                ),
                None,
            ),
        },
        "evidenceFiles": [
            "research-gps-tampering.json",
            "sensor-profile.json",
            "gz-flight-proof-report.json",
            "flight-evidence.json",
            "gazebo-pose.jsonl",
            "physics-engine.txt",
        ],
        "interpretation": (
            "The active local profile forwarded signed Gazebo GPS into Antoine. A single latitude change retaining the captured signature produced no additional GPS acceptance trace, and run-matched independent Gazebo evidence passed the flight and actuator gates. This is a bounded negative result for signature tampering with a test-only key; the sensor source is idealized and this is not physical GPS or production assurance."
            if case == "signature-mutation" and all(gates.values())
            else "The run recorded the bounded signed sample at the sensor ingress trace. The controller build does not expose whether a stale sample was subsequently used by the navigation freshness gate; this result makes no navigation-impact claim."
            if gps_observation_qualified
            else "The sensor tampering experiment is incomplete or one or more independent flight/profile gates failed. Do not treat this as a confirmed rejection result."
        ),
    }
    output = run_dir / "research-gps-tampering-summary.json"
    output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    manifest = build(run_dir)
    write_manifest(run_dir, manifest)
    print(output)
    return 0 if all(gates.values()) else 2


if __name__ == "__main__":
    raise SystemExit(main())
