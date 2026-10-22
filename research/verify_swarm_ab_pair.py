#!/usr/bin/env python3
"""Verify that two swarm reports differ only by one named control setting."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
from pathlib import Path
from typing import Any

from dvd_research.evidence_contract import contains_symlink_component
from summarize_antoine_two_drone_flight import (
    load_host_context_samples,
    load_swarm_config,
    validate_swarm_report,
)

FACTORS = {
    "hold-radius": "arrivalHoldRadiusM",
    "braking-radius": "terminalBrakingRadiusM",
    "predecessor-retention": "retainPredecessorGrant",
}
PROFILE_FACTORS = {
    "hold-radius": "ANTOINE_GZ_ARRIVAL_HOLD_RADIUS_M",
    "braking-radius": "ANTOINE_GZ_TERMINAL_BRAKING_RADIUS_M",
    "predecessor-retention": "ANTOINE_GZ_RETAIN_PREDECESSOR_GRANT",
}
RUN_LOCAL_PROFILE_KEYS = {"ANTOINE_GZ_FLIGHT_NATS_PORT"}
REQUIRED_ARTIFACTS = (
    "gazebo-pose.jsonl",
    "research-provenance-comparison.json",
    "research-antoine-build-receipt.json",
    "research-antoine-process-launches.tsv",
)
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
SWARM_CONFIG_CONTRACT = "damn-vulnerable-drone.antoine-swarm-config"
SWARM_CONFIG_VERSION = "1.3.0"


def is_sha256(value: Any) -> bool:
    return isinstance(value, str) and SHA256_PATTERN.fullmatch(value) is not None


def read_object(path: Path, description: str) -> dict[str, Any]:
    if path.is_symlink():
        raise ValueError(f"cannot read linked {description}: {path}")
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


def validate_config_identity(config: dict[str, Any], label: str) -> None:
    if (
        config.get("contract") != SWARM_CONFIG_CONTRACT
        or config.get("version") != SWARM_CONFIG_VERSION
    ):
        raise ValueError(f"{label} swarm config has an unsupported contract or version")


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
            or not is_sha256(item.get("sourceContentSha256"))
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
            or not is_sha256(item.get("sha256"))
        ):
            raise ValueError(f"provenance is missing pinned executable or input {name}")


def verify_artifacts(run_dir: Path, report: dict[str, Any]) -> None:
    run_dir = Path(run_dir)
    if run_dir.is_symlink():
        raise ValueError("refusing to verify artifacts through a symlinked run directory")
    run_dir = run_dir.resolve(strict=True)
    artifacts = report.get("evidenceArtifacts")
    if not isinstance(artifacts, dict):
        raise ValueError(f"artifact inventory is missing: {run_dir.name}")
    required = [
        *REQUIRED_ARTIFACTS,
        "research-profile.env",
        "research-host-context-pre.json",
        "research-host-context-during.jsonl",
        "research-host-context-post.json",
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
            or contains_symlink_component(run_dir, Path(name))
            or not path.is_file()
        ):
            raise ValueError(f"required raw evidence is missing: {run_dir.name}/{name}")
        try:
            path.resolve(strict=True).relative_to(run_dir)
        except (OSError, ValueError) as error:
            raise ValueError(
                f"required raw evidence escapes run directory: {run_dir.name}/{name}"
            ) from error
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        if digest.hexdigest() != entry.get("sha256"):
            raise ValueError(f"raw evidence changed after report generation: {run_dir.name}/{name}")


def verify_profile_config(run_dir: Path, config: dict[str, Any]) -> dict[str, str]:
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
    try:
        swarm_size = int(values["ANTOINE_SWARM_SIZE"])
        spacing = float(values["ANTOINE_SWARM_SPACING_M"])
        control_rate = float(values["ANTOINE_GZ_FLIGHT_CONTROL_HZ"])
        nats_port = int(values["ANTOINE_GZ_FLIGHT_NATS_PORT"])
    except (KeyError, ValueError) as error:
        raise ValueError(
            f"run profile is missing required runtime settings: {run_dir.name}"
        ) from error
    if (
        not math.isfinite(hold_radius)
        or not math.isfinite(braking_radius)
        or not math.isfinite(spacing)
        or not math.isfinite(control_rate)
        or hold_radius != config["arrivalHoldRadiusM"]
        or braking_radius != config["terminalBrakingRadiusM"]
        or retention not in {"0", "1"}
        or (retention == "1") is not config["retainPredecessorGrant"]
        or swarm_size != len(config["vehicles"])
        or spacing != config["spacingM"]
        or values.get("ANTOINE_SWARM_BEHAVIOR") != config["behavior"]
        or values.get("ANTOINE_SWARM_MAVLINK") != ("1" if config["mavlinkSwarm"] else "0")
        or control_rate <= 0
        or not 1 <= nats_port <= 65535
        or values.get("ANTOINE_GZ_RATE_TRACE") != "1"
    ):
        raise ValueError(f"run profile does not match swarm config: {run_dir.name}")
    return values


def verify_profile_pair(baseline: dict[str, str], variant: dict[str, str], factor: str) -> str:
    factor_key = PROFILE_FACTORS[factor]
    baseline_common = {
        key: value
        for key, value in baseline.items()
        if key != factor_key and key not in RUN_LOCAL_PROFILE_KEYS
    }
    variant_common = {
        key: value
        for key, value in variant.items()
        if key != factor_key and key not in RUN_LOCAL_PROFILE_KEYS
    }
    if baseline_common != variant_common:
        raise ValueError("runtime research profiles differ outside the selected factor")
    return hashlib.sha256(
        json.dumps(baseline_common, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def load_host_contexts(run_dir: Path) -> dict[str, Any]:
    result = {}
    for phase in ("pre", "post"):
        context = read_object(
            run_dir / f"research-host-context-{phase}.json", f"{phase}-run host context"
        )
        if (
            context.get("contract") != "damn-vulnerable-drone.swarm-host-context"
            or context.get("version") != "1.0.0"
            or context.get("runId") != run_dir.name
            or context.get("phase") != phase
            or not isinstance(context.get("capturedAtUtc"), str)
            or not isinstance(context.get("platform"), str)
            or not isinstance(context.get("machine"), str)
            or not is_sha256(context.get("hostId"))
            or type(context.get("logicalCpuCount")) is not int
            or context["logicalCpuCount"] < 1
            or not isinstance(context.get("loadAverage"), dict)
            or not isinstance(context.get("disk"), dict)
            or not isinstance(context.get("processes"), dict)
        ):
            raise ValueError(f"invalid {phase}-run host context: {run_dir.name}")
        loads = context["loadAverage"].get("values")
        if (
            context["loadAverage"].get("status") != "captured"
            or not isinstance(loads, list)
            or len(loads) != 3
            or any(
                type(value) not in {int, float} or not math.isfinite(value) or value < 0
                for value in loads
            )
            or type(context["disk"].get("freeBytes")) is not int
            or context["disk"]["freeBytes"] < 0
            or context["processes"].get("status") != "captured"
            or not isinstance(context["processes"].get("topCpuProcesses"), list)
            or not isinstance(context["processes"].get("simulationAndBuildProcesses"), list)
        ):
            raise ValueError(f"incomplete {phase}-run host context: {run_dir.name}")
        result[phase] = {
            "capturedAtUtc": context["capturedAtUtc"],
            "platform": context.get("platform"),
            "machine": context.get("machine"),
            "hostId": context.get("hostId"),
            "logicalCpuCount": context.get("logicalCpuCount"),
            "loadAverage": context["loadAverage"],
            "diskFreeBytes": context["disk"].get("freeBytes"),
            "memory": context.get("memory"),
            "processes": context["processes"],
        }
    result["during"] = load_host_context_samples(run_dir)
    if result["during"].get("status") != "captured":
        raise ValueError(f"invalid during-run host context: {run_dir.name}")
    if any(
        baseline_value != variant_value
        for baseline_value, variant_value in (
            (result["pre"]["platform"], result["post"]["platform"]),
            (result["pre"]["machine"], result["post"]["machine"]),
            (result["pre"]["hostId"], result["post"]["hostId"]),
            (result["pre"]["logicalCpuCount"], result["post"]["logicalCpuCount"]),
            (result["pre"]["hostId"], result["during"]["hostId"]),
            (result["pre"]["logicalCpuCount"], result["during"]["logicalCpuCount"]),
        )
    ):
        raise ValueError(f"host identity changed during run: {run_dir.name}")
    return result


def require_same_host(baseline: dict[str, Any], variant: dict[str, Any]) -> None:
    phases = ("pre", "during", "post")
    host_ids = {baseline[phase]["hostId"] for phase in phases} | {
        variant[phase]["hostId"] for phase in phases
    }
    if len(host_ids) != 1:
        raise ValueError("the matched pair was not captured on one host")


def summarize_host_load_comparison(
    baseline: dict[str, Any], variant: dict[str, Any]
) -> dict[str, Any]:
    baseline_during = baseline["during"]
    variant_during = variant["during"]
    baseline_load = baseline_during["maxLoadAverage"]
    variant_load = variant_during["maxLoadAverage"]
    return {
        "sampleCounts": {
            "baseline": baseline_during["sampleCount"],
            "variant": variant_during["sampleCount"],
        },
        "maxLoadAverage": {
            "baseline": baseline_load,
            "variant": variant_load,
            "variantMinusBaseline": [
                round(after - before, 3)
                for before, after in zip(baseline_load, variant_load, strict=True)
            ],
        },
        "minimumDiskFreeBytes": {
            "baseline": baseline_during["minDiskFreeBytes"],
            "variant": variant_during["minDiskFreeBytes"],
            "variantMinusBaseline": (
                variant_during["minDiskFreeBytes"] - baseline_during["minDiskFreeBytes"]
            ),
        },
        "peakCpuPercentByProcess": {
            "baseline": baseline_during["peakCpuPercentByProcess"],
            "variant": variant_during["peakCpuPercentByProcess"],
        },
        "simulationAndBuildProcessNames": {
            "baseline": baseline_during["simulationAndBuildProcessNames"],
            "variant": variant_during["simulationAndBuildProcessNames"],
        },
        "interpretation": (
            "Descriptive host context only; this does not qualify equivalent load or establish "
            "a causal effect."
        ),
    }


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
                "controllerStateCoalescing": vehicle["controllerStateCoalescing"],
                "missionProgressFailure": vehicle["missionProgressFailure"],
            }
            for callsign, vehicle in report["vehicles"].items()
        },
    }


def verify_pair(baseline_dir: Path, variant_dir: Path, factor: str) -> dict[str, Any]:
    if factor not in FACTORS:
        raise ValueError(f"unsupported factor: {factor}")
    baseline_dir, variant_dir = Path(baseline_dir), Path(variant_dir)
    if baseline_dir.is_symlink() or variant_dir.is_symlink():
        raise ValueError("baseline and variant run directories cannot be symlinks")
    baseline_dir, variant_dir = baseline_dir.resolve(strict=True), variant_dir.resolve(strict=True)
    if not baseline_dir.is_dir() or not variant_dir.is_dir():
        raise ValueError("baseline and variant must be run directories")
    if baseline_dir == variant_dir:
        raise ValueError("baseline and variant must be different run directories")
    baseline_config_raw = read_object(
        baseline_dir / "research-swarm-config.json", "baseline swarm config"
    )
    variant_config_raw = read_object(
        variant_dir / "research-swarm-config.json", "variant swarm config"
    )
    validate_config_identity(baseline_config_raw, "baseline")
    validate_config_identity(variant_config_raw, "variant")
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
    profile_values = []
    for run_dir, report, config in (
        (baseline_dir, baseline_report, baseline_config),
        (variant_dir, variant_report, variant_config),
    ):
        if report.get("runOwnedSourceBuildInputsPresent") is not True:
            raise ValueError(f"run-owned source/build inputs are not qualified: {run_dir.name}")
        verify_artifacts(run_dir, report)
        profile_values.append(verify_profile_config(run_dir, config))
    shared_profile_sha256 = verify_profile_pair(profile_values[0], profile_values[1], factor)
    baseline_host = load_host_contexts(baseline_dir)
    variant_host = load_host_contexts(variant_dir)
    require_same_host(baseline_host, variant_host)
    host_load_comparison = summarize_host_load_comparison(baseline_host, variant_host)

    return {
        "contract": "damn-vulnerable-drone.antoine-swarm-ab-pair",
        "version": "1.1.0",
        "status": "matched_inputs",
        "factor": factor,
        "configKey": key,
        "baselineValue": baseline_value,
        "variantValue": variant_value,
        "sharedConfigSha256": shared_config_sha256,
        "sharedProfileSha256": shared_profile_sha256,
        "sharedProvenance": baseline_report["provenance"],
        "hostContext": {"baseline": baseline_host, "variant": variant_host},
        "hostLoadComparison": host_load_comparison,
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
