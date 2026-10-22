#!/usr/bin/env python3
"""Verify that two swarm reports differ only by one named control setting."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
from typing import Any

from summarize_antoine_two_drone_flight import load_swarm_config, validate_swarm_report

FACTORS = {
    "hold-radius": "arrivalHoldRadiusM",
    "braking-radius": "terminalBrakingRadiusM",
    "predecessor-retention": "retainPredecessorGrant",
}
REQUIRED_ARTIFACTS = (
    "gazebo-pose.jsonl",
    "research-provenance-comparison.json",
    "research-antoine-build-receipt.json",
    "research-antoine-process-launches.tsv",
)


def read_object(path: Path, description: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"cannot read {description}: {path}") from error
    if not isinstance(value, dict):
        raise ValueError(f"{description} must be a JSON object: {path}")
    return value


def config_pair(
    baseline: dict[str, Any], variant: dict[str, Any], factor: str
) -> tuple[str, Any, Any, str]:
    key = FACTORS[factor]
    if key not in baseline or key not in variant:
        raise ValueError(f"both configs must explicitly record {key}")
    baseline_value, variant_value = baseline[key], variant[key]
    if baseline_value == variant_value or type(baseline_value) is not type(variant_value):
        raise ValueError(f"the selected factor {key} must change between runs")
    if factor in {"hold-radius", "braking-radius"} and (
        isinstance(baseline_value, bool)
        or not isinstance(baseline_value, (int, float))
        or not math.isfinite(baseline_value)
        or isinstance(variant_value, bool)
        or not isinstance(variant_value, (int, float))
        or not math.isfinite(variant_value)
    ):
        raise ValueError(f"{key} must contain finite numeric values")
    baseline_common = {name: value for name, value in baseline.items() if name != key}
    variant_common = {name: value for name, value in variant.items() if name != key}
    if baseline_common != variant_common:
        raise ValueError("swarm configs differ in settings outside the selected factor")
    if baseline.get("fault", "none") != "none" or baseline.get("mavlinkSwarm") is True:
        raise ValueError("the matched attitude experiment requires a clean non-MAVLink swarm")
    if len(baseline.get("vehicles", [])) != 2:
        raise ValueError("the matched attitude experiment requires exactly two vehicles")
    digest = hashlib.sha256(
        json.dumps(baseline_common, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return key, baseline_value, variant_value, digest


def verify_provenance(baseline: dict[str, Any], variant: dict[str, Any]) -> None:
    if (
        not isinstance(baseline, dict)
        or not isinstance(variant, dict)
        or baseline != variant
        or baseline.get("status") != "stable"
    ):
        raise ValueError("run provenance is unstable or differs between the pair")
    trees = baseline.get("sourceTrees")
    executables = baseline.get("executables")
    if not isinstance(trees, dict) or not isinstance(executables, dict):
        raise ValueError("stable provenance is missing source or executable inventories")
    for name in ("antoine", "gazebo_distribution", "gazebo_sim_assets", "dvd_research"):
        item = trees.get(name)
        if (
            not isinstance(item, dict)
            or item.get("available") is not True
            or not isinstance(item.get("revision"), str)
            or not item["revision"]
            or not isinstance(item.get("sourceContentSha256"), str)
            or len(item["sourceContentSha256"]) != 64
        ):
            raise ValueError(f"provenance is missing source tree {name}")
    for name in (
        "gazebo_binary",
        "source_world",
        "runtime_world_overlay",
        "x500_model",
        "antoine_sitl_binary",
        "antoine_build_receipt",
        "antoine_source_snapshot_receipt",
        "gazebo_source_snapshot_receipt",
        "gazebo_model_assets",
    ):
        item = executables.get(name)
        if (
            not isinstance(item, dict)
            or item.get("available") is not True
            or not isinstance(item.get("sha256"), str)
            or len(item["sha256"]) != 64
        ):
            raise ValueError(f"provenance is missing pinned executable or input {name}")


def verify_artifacts(run_dir: Path, report: dict[str, Any]) -> None:
    artifacts = report["evidenceArtifacts"]
    required = [
        *REQUIRED_ARTIFACTS,
        "research-profile.env",
        "research-swarm-config.json",
        "research-swarm-plan.json",
    ]
    for callsign in report["vehicles"]:
        required.extend(
            (
                f"flight-evidence-{callsign}.json",
                f"gz-flight-proof-report-{callsign}.json",
                f"logs/antoine-controller-{callsign}.log",
            )
        )
    for name in required:
        path = run_dir / name
        entry = artifacts.get(name)
        if (
            not isinstance(entry, dict)
            or entry.get("available") is not True
            or path.is_symlink()
            or not path.is_file()
        ):
            raise ValueError(f"required raw evidence is missing: {run_dir.name}/{name}")
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        if digest.hexdigest() != entry.get("sha256"):
            raise ValueError(f"raw evidence changed after report generation: {run_dir.name}/{name}")


def verify_profile_config(run_dir: Path, config: dict[str, Any]) -> None:
    profile_path = run_dir / "research-profile.env"
    try:
        lines = profile_path.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise ValueError(f"cannot read run profile: {run_dir.name}") from error
    values: dict[str, str] = {}
    for line in lines:
        key, separator, value = line.partition("=")
        if not separator or not key or key in values:
            raise ValueError(f"invalid run profile entry: {run_dir.name}")
        values[key] = value
    try:
        hold_radius = float(values["ANTOINE_GZ_ARRIVAL_HOLD_RADIUS_M"])
        braking_radius = float(values["ANTOINE_GZ_TERMINAL_BRAKING_RADIUS_M"])
    except (KeyError, ValueError) as error:
        raise ValueError(f"run profile is missing swarm control values: {run_dir.name}") from error
    retention = values.get("ANTOINE_GZ_RETAIN_PREDECESSOR_GRANT")
    if (
        not math.isfinite(hold_radius)
        or not math.isfinite(braking_radius)
        or hold_radius != config["arrivalHoldRadiusM"]
        or braking_radius != config["terminalBrakingRadiusM"]
        or retention not in {"0", "1"}
        or (retention == "1") is not config["retainPredecessorGrant"]
    ):
        raise ValueError(f"run profile does not match swarm config: {run_dir.name}")


def summarize_outcome(report: dict[str, Any]) -> dict[str, Any]:
    return {
        "runId": report["runId"],
        "status": report["status"],
        "behaviorStatus": report["behaviorStatus"],
        "baselineStatus": report["baselineStatus"],
        "baselineFailedReasons": report["baselineFailedReasons"],
        "baselineInconclusiveReasons": report["baselineInconclusiveReasons"],
        "vehicles": {
            callsign: {
                "model": vehicle["model"],
                "attitudeQuality": vehicle["attitudeQuality"],
                "allWaypointsReached": vehicle["allWaypointsReached"],
                "waypoints": vehicle["waypoints"],
                "controllerStateCadence": vehicle["controllerStateCadence"],
                "missionProgressFailure": vehicle["missionProgressFailure"],
            }
            for callsign, vehicle in report["vehicles"].items()
        },
    }


def verify_pair(baseline_dir: Path, variant_dir: Path, factor: str) -> dict[str, Any]:
    if factor not in FACTORS:
        raise ValueError(f"unsupported factor: {factor}")
    baseline_dir, variant_dir = baseline_dir.resolve(), variant_dir.resolve()
    if baseline_dir == variant_dir:
        raise ValueError("baseline and variant must be different run directories")
    baseline_config_raw = read_object(
        baseline_dir / "research-swarm-config.json", "baseline swarm config"
    )
    variant_config_raw = read_object(
        variant_dir / "research-swarm-config.json", "variant swarm config"
    )
    baseline_config = load_swarm_config(baseline_dir)
    variant_config = load_swarm_config(variant_dir)
    key, baseline_value, variant_value, shared_config_sha256 = config_pair(
        baseline_config_raw, variant_config_raw, factor
    )
    if baseline_config["vehicles"] != variant_config["vehicles"]:
        raise ValueError("normalized vehicle rosters differ")

    baseline_report = read_object(
        baseline_dir / "research-swarm-flight.json", "baseline swarm report"
    )
    variant_report = read_object(variant_dir / "research-swarm-flight.json", "variant swarm report")
    validate_swarm_report(baseline_report, baseline_dir.name)
    validate_swarm_report(variant_report, variant_dir.name)
    for raw_config, report in (
        (baseline_config_raw, baseline_report),
        (variant_config_raw, variant_report),
    ):
        if any(report.get(control) != raw_config.get(control) for control in FACTORS.values()):
            raise ValueError("swarm report control settings do not match the run config")
    verify_provenance(baseline_report.get("provenance", {}), variant_report.get("provenance", {}))
    for run_dir, report, config in (
        (baseline_dir, baseline_report, baseline_config),
        (variant_dir, variant_report, variant_config),
    ):
        if report.get("runOwnedSourceBuildInputsPresent") is not True:
            raise ValueError(f"run-owned source/build inputs are not qualified: {run_dir.name}")
        verify_artifacts(run_dir, report)
        verify_profile_config(run_dir, config)

    return {
        "contract": "damn-vulnerable-drone.antoine-swarm-ab-pair",
        "version": "1.0.0",
        "status": "matched_inputs",
        "factor": factor,
        "configKey": key,
        "baselineValue": baseline_value,
        "variantValue": variant_value,
        "sharedConfigSha256": shared_config_sha256,
        "sharedProvenance": baseline_report["provenance"],
        "baseline": summarize_outcome(baseline_report),
        "variant": summarize_outcome(variant_report),
        "interpretation": (
            "Inputs match except for the selected control. This artifact verifies pair integrity; "
            "it does not assert that the selected control caused an outcome difference."
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--variant", type=Path, required=True)
    parser.add_argument("--factor", choices=FACTORS, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        result = verify_pair(args.baseline, args.variant, args.factor)
    except ValueError as error:
        parser.error(str(error))
    rendered = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.write_text(rendered, encoding="utf-8")
    else:
        print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
