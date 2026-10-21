#!/usr/bin/env python3
"""Aggregate per-vehicle flight gates and all-pairs synchronized separation."""

from __future__ import annotations

import hashlib
import html
import itertools
import json
import math
import sys
from pathlib import Path
from typing import Any

from actuator_lifecycle import disarmed_status
from swarm_behavior import build_plan, evaluate_plan

DEFAULT_VEHICLES = [
    {"callsign": "drone-1", "model": "isr"},
    {"callsign": "drone-2", "model": "fpv-a"},
]
DEFAULT_SPACING_M = 8.0
SWARM_REPORT_VERSION = "1.12.0"
POSE_PAIR_TOLERANCE_SECONDS = 0.05
MINIMUM_SEPARATION_METERS = 1.0
MINIMUM_POSE_SAMPLES_PER_VEHICLE = 20
MINIMUM_POSE_COVERAGE_SECONDS = 1.0
MAXIMUM_POSE_GAP_SECONDS = 0.5
MAXIMUM_CONTROL_TICKS_PER_STATE_SAMPLE = 1.1
MINIMUM_CADENCE_TELEMETRY_SAMPLES = 20
MAX_CROSS_VEHICLE_POSE_EFFECT_M = 1.0
MAX_CROSS_VEHICLE_EFFECT_WINDOW_SECONDS = 2.0
TRAJECTORY_COLORS = ["#1677ff", "#e4572e", "#2a9d55", "#8e5bd9", "#d49a00"]


def load_swarm_config(run_dir: Path) -> dict[str, Any]:
    config_path = run_dir / "research-swarm-config.json"
    if not config_path.is_file():
        return {
            "vehicles": DEFAULT_VEHICLES,
            "spacingM": DEFAULT_SPACING_M,
            "behavior": "lanes",
            "mavlinkSwarm": False,
        }
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
        vehicles = config["vehicles"]
    except (OSError, json.JSONDecodeError, KeyError, TypeError) as error:
        raise ValueError("invalid swarm run configuration") from error
    if not isinstance(vehicles, list) or not 2 <= len(vehicles) <= 5:
        raise ValueError("swarm run configuration must declare 2..5 vehicles")
    normalized = []
    for item in vehicles:
        if not isinstance(item, dict):
            raise ValueError("invalid swarm vehicle entry")
        callsign, model = item.get("callsign"), item.get("model")
        if not isinstance(callsign, str) or not isinstance(model, str):
            raise ValueError("swarm vehicle entries need callsign and model")
        normalized.append({"callsign": callsign, "model": model})
    if len({item["callsign"] for item in normalized}) != len(normalized):
        raise ValueError("duplicate swarm callsign")
    if len({item["model"] for item in normalized}) != len(normalized):
        raise ValueError("duplicate swarm model")
    spacing = config.get("spacingM", DEFAULT_SPACING_M)
    if not isinstance(spacing, (int, float)) or isinstance(spacing, bool):
        raise ValueError("swarm spacing must be numeric")
    behavior = config.get("behavior", "lanes")
    if not isinstance(behavior, str):
        raise ValueError("swarm behavior must be a string")
    return {
        "vehicles": normalized,
        "spacingM": float(spacing),
        "behavior": behavior,
        "mavlinkSwarm": config.get("mavlinkSwarm") is True,
        "fault": config.get("fault", "none"),
    }


def load_swarm_plan(
    run_dir: Path, config: dict[str, Any], vehicles: list[dict[str, str]]
) -> dict[str, Any]:
    plan_path = run_dir / "research-swarm-plan.json"
    if plan_path.is_file():
        try:
            plan = json.loads(plan_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ValueError("invalid swarm plan") from error
        if not isinstance(plan, dict):
            raise ValueError("invalid swarm plan")
        plan_vehicles = [
            vehicle for vehicle in plan.get("vehicles", []) if isinstance(vehicle, dict)
        ]
        if [vehicle.get("callsign") for vehicle in plan_vehicles] != [
            vehicle["callsign"] for vehicle in vehicles
        ]:
            raise ValueError("swarm plan does not match the configured roster")
        if [vehicle.get("model") for vehicle in plan_vehicles] != [
            vehicle["model"] for vehicle in vehicles
        ]:
            raise ValueError("swarm plan does not match the configured models")
        if plan.get("behavior") != config["behavior"]:
            raise ValueError("swarm plan behaviour does not match the run configuration")
        return plan
    # Older runs predate the plan artifact. Reproduce the lanes plan they used
    # so their separation evidence remains readable, and label it as inferred.
    plan = build_plan(
        "lanes",
        len(vehicles),
        float(config["spacingM"]),
        models=[vehicle["model"] for vehicle in vehicles],
    )
    plan["planSource"] = "inferred-lanes-from-config"
    return plan


def load_pose_samples(
    run_dir: Path, models: set[str]
) -> dict[str, list[tuple[float, float, float, float]]]:
    samples = {model: [] for model in models}
    pose_path = run_dir / "gazebo-pose.jsonl"
    if not pose_path.is_file():
        return samples
    with pose_path.open(encoding="utf-8", errors="replace") as stream:
        for line in stream:
            try:
                frame = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(frame, dict):
                continue
            stamp = frame.get("header", {}).get("stamp", {})
            try:
                timestamp = int(stamp.get("sec", 0)) + int(stamp.get("nsec", 0)) / 1e9
            except (AttributeError, TypeError, ValueError):
                continue
            poses = frame.get("pose")
            if not isinstance(poses, list):
                poses = [frame]
            for pose in poses:
                if not isinstance(pose, dict) or pose.get("name") not in samples:
                    continue
                position = pose.get("position", {})
                try:
                    sample = (
                        timestamp,
                        float(position["x"]),
                        float(position["y"]),
                        float(position["z"]),
                    )
                except (KeyError, TypeError, ValueError):
                    continue
                samples[pose["name"]].append(sample)
    return samples


def measure_pose_stream(
    samples: list[tuple[float, float, float, float]],
) -> dict[str, Any]:
    timestamps = [sample[0] for sample in samples]
    deltas = [later - earlier for earlier, later in itertools.pairwise(timestamps)]
    regressions = sum(delta < 0 for delta in deltas)
    duplicates = sum(delta == 0 for delta in deltas)
    positive_gaps = [delta for delta in deltas if delta > 0]
    duration = timestamps[-1] - timestamps[0] if len(timestamps) > 1 else 0.0
    max_gap = max(positive_gaps, default=None)
    average_rate = (len(positive_gaps) / duration) if duration > 0 else None
    enough_samples = len(samples) >= MINIMUM_POSE_SAMPLES_PER_VEHICLE
    enough_coverage = duration >= MINIMUM_POSE_COVERAGE_SECONDS
    fresh = max_gap is not None and max_gap <= MAXIMUM_POSE_GAP_SECONDS
    monotonic = regressions == 0 and duplicates == 0
    return {
        "sampleCount": len(samples),
        "firstSimulationTimeSeconds": timestamps[0] if timestamps else None,
        "lastSimulationTimeSeconds": timestamps[-1] if timestamps else None,
        "coverageSeconds": round(duration, 6),
        "averageSampleRateHz": round(average_rate, 3) if average_rate is not None else None,
        "maximumSampleGapSeconds": round(max_gap, 6) if max_gap is not None else None,
        "timestampRegressionCount": regressions,
        "duplicateTimestampCount": duplicates,
        "orderedUniqueTimestamps": monotonic,
        "enoughSamples": enough_samples,
        "enoughCoverage": enough_coverage,
        "freshEnough": fresh,
        "passed": monotonic and enough_samples and enough_coverage and fresh,
    }


def write_trajectory_svg(
    run_dir: Path,
    vehicles: list[dict[str, str]],
    samples: dict[str, list[tuple[float, float, float, float]]],
) -> bool:
    """Write a dependency-free, top-down plot from independent Gazebo poses."""
    series = [(vehicle, samples[vehicle["model"]]) for vehicle in vehicles]
    series = [(vehicle, points) for vehicle, points in series if points]
    if len(series) != len(vehicles):
        return False
    x_values = [point[1] for _, points in series for point in points]
    y_values = [point[2] for _, points in series for point in points]
    x_span, y_span = max(x_values) - min(x_values), max(y_values) - min(y_values)
    x_pad, y_pad = max(x_span * 0.08, 1.0), max(y_span * 0.12, 1.0)
    x_min, x_max = min(x_values) - x_pad, max(x_values) + x_pad
    y_min, y_max = min(y_values) - y_pad, max(y_values) + y_pad
    plot = {"left": 100, "top": 90, "width": 820, "height": 470}

    def project(x: float, y: float) -> tuple[float, float]:
        px = plot["left"] + (x - x_min) / (x_max - x_min) * plot["width"]
        py = plot["top"] + (y_max - y) / (y_max - y_min) * plot["height"]
        return px, py

    elements = [
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1000 680" role="img" aria-labelledby="title description">',
        '<title id="title">Gazebo swarm trajectories</title>',
        '<desc id="description">Top-down X/Y trajectories measured from independent Gazebo world poses.</desc>',
        '<rect width="1000" height="680" fill="#fff"/>',
        '<text x="100" y="38" font-family="sans-serif" font-size="23" font-weight="700">Swarm flight · Gazebo pose truth</text>',
        f'<text x="100" y="64" font-family="sans-serif" font-size="13" fill="#52606d">{html.escape(run_dir.name)} · X east (m) · Y north (m)</text>',
    ]
    for tick in range(5):
        fraction = tick / 4
        x = x_min + fraction * (x_max - x_min)
        y = y_min + fraction * (y_max - y_min)
        px, _ = project(x, y_min)
        _, py = project(x_min, y)
        elements.extend(
            [
                f'<line x1="{px:.1f}" y1="{plot["top"]}" x2="{px:.1f}" y2="{plot["top"] + plot["height"]}" stroke="#e5e9ef"/>',
                f'<line x1="{plot["left"]}" y1="{py:.1f}" x2="{plot["left"] + plot["width"]}" y2="{py:.1f}" stroke="#e5e9ef"/>',
                f'<text x="{px:.1f}" y="{plot["top"] + plot["height"] + 22}" text-anchor="middle" font-family="sans-serif" font-size="12" fill="#52606d">{x:.1f}</text>',
                f'<text x="{plot["left"] - 12}" y="{py + 4:.1f}" text-anchor="end" font-family="sans-serif" font-size="12" fill="#52606d">{y:.1f}</text>',
            ]
        )
    elements.append(
        f'<rect x="{plot["left"]}" y="{plot["top"]}" width="{plot["width"]}" height="{plot["height"]}" fill="none" stroke="#52606d"/>'
    )
    legend_x, legend_y = 100, 620
    for index, (vehicle, points) in enumerate(series):
        color = TRAJECTORY_COLORS[index % len(TRAJECTORY_COLORS)]
        stride = max(1, math.ceil(len(points) / 700))
        sampled = points[::stride]
        if sampled[-1] != points[-1]:
            sampled.append(points[-1])
        coordinates = " ".join(
            f"{project(point[1], point[2])[0]:.1f},{project(point[1], point[2])[1]:.1f}"
            for point in sampled
        )
        start_x, start_y = project(points[0][1], points[0][2])
        end_x, end_y = project(points[-1][1], points[-1][2])
        label = html.escape(f"{vehicle['callsign']} · {vehicle['model']}")
        elements.extend(
            [
                f'<polyline points="{coordinates}" fill="none" stroke="{color}" stroke-width="3" stroke-linejoin="round" stroke-linecap="round"/>',
                f'<circle cx="{start_x:.1f}" cy="{start_y:.1f}" r="5" fill="#fff" stroke="{color}" stroke-width="3"><title>{label} start</title></circle>',
                f'<circle cx="{end_x:.1f}" cy="{end_y:.1f}" r="5" fill="{color}"><title>{label} finish</title></circle>',
                f'<line x1="{legend_x}" y1="{legend_y}" x2="{legend_x + 24}" y2="{legend_y}" stroke="{color}" stroke-width="4"/>',
                f'<text x="{legend_x + 32}" y="{legend_y + 5}" font-family="sans-serif" font-size="14">{label}</text>',
            ]
        )
        legend_x += 190
    elements.append(
        '<text x="100" y="660" font-family="sans-serif" font-size="12" fill="#52606d">Lines show recorded paths; hollow marker = start, filled marker = finish. Plot excludes altitude.</text>'
    )
    elements.append("</svg>")
    (run_dir / "research-swarm-trajectory.svg").write_text(
        "\n".join(elements) + "\n", encoding="utf-8"
    )
    return True


def measure_pair(
    first: list[tuple[float, float, float, float]],
    second: list[tuple[float, float, float, float]],
) -> dict[str, Any]:
    closest_pair = None
    min_separation = math.inf
    synchronized_pairs: list[tuple[tuple[float, ...], tuple[float, ...], float]] = []
    first_index = 0
    second_index = 0
    while first_index < len(first) and second_index < len(second):
        sample, other = first[first_index], second[second_index]
        time_delta = sample[0] - other[0]
        if abs(time_delta) <= POSE_PAIR_TOLERANCE_SECONDS:
            synchronized_pairs.append((sample, other, abs(time_delta)))
            first_index += 1
            second_index += 1
        elif time_delta < 0:
            first_index += 1
        else:
            second_index += 1

    for sample, other, time_skew in synchronized_pairs:
        distance = math.dist(sample[1:], other[1:])
        if distance < min_separation:
            min_separation = distance
            closest_pair = {
                "simulationTimeSeconds": sample[0],
                "firstSimulationTimeSeconds": sample[0],
                "secondSimulationTimeSeconds": other[0],
                "clockAlignmentErrorSeconds": round(time_skew, 6),
                "firstWorldPositionM": list(sample[1:]),
                "secondWorldPositionM": list(other[1:]),
            }
    pair_skews = [item[2] for item in synchronized_pairs]
    pair_coverage = (
        synchronized_pairs[-1][0][0] - synchronized_pairs[0][0][0]
        if len(synchronized_pairs) > 1
        else 0.0
    )
    adequate = (
        len(synchronized_pairs) >= MINIMUM_POSE_SAMPLES_PER_VEHICLE
        and pair_coverage >= MINIMUM_POSE_COVERAGE_SECONDS
        and bool(pair_skews)
        and max(pair_skews) <= POSE_PAIR_TOLERANCE_SECONDS
    )
    return {
        "synchronizedPosePairCount": len(synchronized_pairs),
        "synchronizedCoverageSeconds": round(pair_coverage, 6),
        "maximumClockAlignmentErrorSeconds": round(max(pair_skews), 6) if pair_skews else None,
        "minimumSeparationM": round(min_separation, 3) if math.isfinite(min_separation) else None,
        "closestSynchronizedPosePair": closest_pair,
        "measurementStatus": "measured"
        if adequate and closest_pair is not None
        else "inconclusive",
    }


def load_flight_report(run_dir: Path, callsign: str) -> dict[str, Any]:
    path = run_dir / f"gz-flight-proof-report-{callsign}.json"
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(report, dict):
        return {}
    evidence_path = run_dir / f"flight-evidence-{callsign}.json"
    try:
        authority_evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        authority_evidence = None
    if (
        isinstance(authority_evidence, dict)
        and authority_evidence.get("callsign") == callsign
        and isinstance(authority_evidence.get("telemetry"), list)
    ):
        report["telemetry"] = authority_evidence["telemetry"]
    return report


def synchronized_waypoint_snapshot(
    vehicles: list[dict[str, str]],
    samples_by_callsign: dict[str, list[tuple[float, float, float, float]]],
    reports: dict[str, dict[str, Any]],
) -> tuple[dict[str, list[tuple[float, float, float, float]]], float | None]:
    """Trim each pose stream at the common time all final waypoints were reached."""
    arrivals = []
    for vehicle in vehicles:
        callsign = vehicle["callsign"]
        legs = reports.get(callsign, {}).get("measured", {}).get("legs", [])
        if not isinstance(legs, list) or not legs:
            return {}, None
        final_leg = legs[-1]
        if not isinstance(final_leg, dict) or final_leg.get("reached") is not True:
            return {}, None
        elapsed_arrival = final_leg.get("simSeconds")
        timing_reference = reports.get(callsign, {}).get("timingReference")
        origin = (
            timing_reference.get("simulationTimeOriginSeconds")
            if isinstance(timing_reference, dict)
            and timing_reference.get("source") == "first-valid-gazebo-pose"
            else None
        )
        if (
            not isinstance(elapsed_arrival, (int, float))
            or isinstance(elapsed_arrival, bool)
            or not isinstance(origin, (int, float))
            or isinstance(origin, bool)
        ):
            return {}, None
        if not math.isfinite(float(elapsed_arrival)) or not math.isfinite(float(origin)):
            return {}, None
        arrivals.append(float(origin) + float(elapsed_arrival))
    common_time = max(arrivals)
    synchronized = {}
    for vehicle in vehicles:
        callsign = vehicle["callsign"]
        samples = samples_by_callsign.get(callsign, [])
        candidates = [index for index, sample in enumerate(samples) if sample[0] <= common_time]
        if not candidates:
            return {}, None
        index = candidates[-1]
        if common_time - samples[index][0] > MAXIMUM_POSE_GAP_SECONDS:
            return {}, None
        synchronized[callsign] = samples[: index + 1]
    return synchronized, common_time


def mission_progress_status(
    evidence: dict[str, Any] | None,
    callsign: str,
    run_id: str,
    waypoint_reports: list[Any],
    actuator_records: list[dict[str, Any]],
    controller_outcomes: list[dict[str, Any]],
) -> dict[str, Any]:
    """Validate authority progress against the evaluator's reached waypoints."""
    if not isinstance(evidence, dict) or not isinstance(evidence.get("missionProgress"), list):
        return {
            "status": "unavailable",
            "eventCount": 0,
            "waypointTransitions": [],
            "transitionBindings": [],
            "missingEvaluatorTransitions": [],
            "invalidTransitionWaypointIndices": [],
            "evaluatorReachedWaypoints": [],
            "unconfirmedTransitions": [],
            "invalidReasons": [],
            "lastProgress": None,
            "events": [],
        }
    events = evidence["missionProgress"]
    valid = []
    last_sequence = 0
    last_timestamp = 0
    invalid_reasons = []
    transition_bindings = []
    invalid_transition_waypoints = []
    for event in events:
        if not isinstance(event, dict):
            invalid_reasons.append("malformed-event")
            continue
        sequence = event.get("eventSequence")
        if (
            event.get("runId") != run_id
            or event.get("callsign") != callsign
            or not isinstance(sequence, int)
            or isinstance(sequence, bool)
            or sequence <= last_sequence
            or not isinstance(event.get("atUnixMs"), int)
            or isinstance(event.get("atUnixMs"), bool)
            or event.get("atUnixMs", 0) < last_timestamp
            or isinstance(event.get("waypointIndex"), int) is False
            or isinstance(event.get("waypointIndex"), bool)
            or any(
                not isinstance(event.get(key), int)
                or isinstance(event.get(key), bool)
                or event.get(key) < 0
                for key in ("grantSequence", "setpointSequence", "vigieAdmissionSequence")
            )
            or event.get("event") not in {"grant_issued", "setpoint_published", "waypoint_reached"}
        ):
            invalid_reasons.append("identity-sequence-or-binding-invalid")
            continue
        last_sequence = sequence
        last_timestamp = event["atUnixMs"]
        valid.append(event)
    reached_by_authority = [
        event["waypointIndex"] for event in valid if event.get("event") == "waypoint_reached"
    ]
    reached_by_evaluator = [
        item.get("index")
        for item in waypoint_reports
        if isinstance(item, dict) and item.get("reached") is True
    ]
    transition_waypoints = [
        event.get("waypointIndex") for event in valid if event.get("event") == "waypoint_reached"
    ]
    mismatches = [index for index in reached_by_authority if index not in reached_by_evaluator]
    missing_transitions = [
        index for index in reached_by_evaluator[:-1] if index not in transition_waypoints
    ]
    if transition_waypoints != sorted(set(transition_waypoints)):
        invalid_reasons.append("waypoint-transition-order-invalid")
    for index, event in enumerate(valid):
        if event.get("event") != "waypoint_reached":
            continue
        next_index = event.get("advancingToWaypointIndex")
        next_target = event.get("nextTargetEnuM")
        if next_index != event.get("waypointIndex", -2) + 1 or not valid_vec3(next_target):
            invalid_reasons.append("waypoint-transition-target-invalid")
            invalid_transition_waypoints.append(event.get("waypointIndex"))
            continue
        next_grant = next(
            (
                candidate
                for candidate in valid[index + 1 :]
                if candidate.get("event") == "grant_issued"
                and candidate.get("waypointIndex") == next_index
                and candidate.get("targetEnuM") == next_target
                and candidate.get("grantSequence", 0) > event.get("grantSequence", 0)
            ),
            None,
        )
        next_setpoint = next(
            (
                candidate
                for candidate in valid[index + 1 :]
                if next_grant is not None
                and candidate["eventSequence"] > next_grant["eventSequence"]
                and candidate.get("event") == "setpoint_published"
                and candidate.get("waypointIndex") == next_index
                and candidate.get("targetEnuM") == next_target
                and candidate.get("grantSequence") == next_grant.get("grantSequence")
                and candidate.get("setpointSequence", 0) > next_grant.get("setpointSequence", 0)
                and candidate.get("vigieAdmissionSequence", 0) > 0
            ),
            None,
        )
        if next_grant is None:
            invalid_reasons.append("transition-grant-not-correlated")
            invalid_transition_waypoints.append(event["waypointIndex"])
        elif next_setpoint is None:
            invalid_reasons.append("transition-setpoint-not-correlated")
            invalid_transition_waypoints.append(event["waypointIndex"])
        else:
            transition_bindings.append(
                {
                    "reachedWaypointIndex": event["waypointIndex"],
                    "nextWaypointIndex": next_index,
                    "grantSequence": next_grant["grantSequence"],
                    "setpointSequence": next_setpoint["setpointSequence"],
                    "vigieAdmissionSequence": next_setpoint["vigieAdmissionSequence"],
                    "grantEventSequence": next_grant["eventSequence"],
                    "setpointEventSequence": next_setpoint["eventSequence"],
                    "setpointAtUnixMs": next_setpoint["atUnixMs"],
                    "targetEnuM": next_target,
                }
            )
    for binding in transition_bindings:
        target_mm = [round(component * 1000) for component in binding["targetEnuM"]]
        matching_commands = []
        mismatched_commands = []
        for record in actuator_records:
            grant = record.get("executionGrant")
            if (
                record.get("stage") != "authorized-command"
                or record.get("vehicleId") != callsign
                or record.get("runId") != run_id
                or not isinstance(grant, dict)
                or grant.get("grantSequence") != binding["grantSequence"]
            ):
                continue
            if grant.get("targetEnuMm") == target_mm:
                if (
                    isinstance(record.get("recordedAtUnixMs"), int)
                    and record["recordedAtUnixMs"] >= binding["setpointAtUnixMs"]
                    and record.get("authority") == "verified-core-grant"
                ):
                    matching_commands.append(record)
            else:
                mismatched_commands.append(record)
        binding["actuatorCommandCount"] = len(matching_commands)
        binding["firstActuatorSequence"] = (
            min(record["sequence"] for record in matching_commands)
            if matching_commands
            and all(isinstance(record.get("sequence"), int) for record in matching_commands)
            else None
        )
        if mismatched_commands:
            invalid_reasons.append("transition-actuator-target-mismatch")
            invalid_transition_waypoints.append(binding["reachedWaypointIndex"])
        if not matching_commands:
            invalid_reasons.append("transition-actuator-not-correlated")
            invalid_transition_waypoints.append(binding["reachedWaypointIndex"])
        matching_controller_outcomes = [
            outcome
            for outcome in controller_outcomes
            if outcome.get("vigieSequence") == binding["vigieAdmissionSequence"]
        ]
        accepted_controller_outcomes = [
            outcome
            for outcome in matching_controller_outcomes
            if outcome.get("decision") == "execute"
        ]
        binding["controllerDecisionEvidence"] = {
            "status": "measured" if accepted_controller_outcomes else "unavailable",
            "log": f"logs/antoine-controller-{callsign}.log",
            "executeDecisionCount": len(accepted_controller_outcomes),
            "rejectedDecisionCount": len(matching_controller_outcomes)
            - len(accepted_controller_outcomes),
            "firstExecuteLine": (
                accepted_controller_outcomes[0]["lineNumber"]
                if accepted_controller_outcomes
                else None
            ),
        }
        if not accepted_controller_outcomes:
            invalid_reasons.append("transition-controller-admission-not-confirmed")
            invalid_transition_waypoints.append(binding["reachedWaypointIndex"])
    status = "invalid" if invalid_reasons or mismatches else "measured" if valid else "unavailable"
    return {
        "status": status,
        "eventCount": len(valid),
        "waypointTransitions": reached_by_authority,
        "transitionBindings": transition_bindings,
        "missingEvaluatorTransitions": missing_transitions,
        "invalidTransitionWaypointIndices": sorted(set(invalid_transition_waypoints)),
        "evaluatorReachedWaypoints": reached_by_evaluator,
        "unconfirmedTransitions": mismatches,
        "invalidReasons": sorted(set(invalid_reasons)),
        "lastProgress": valid[-1] if valid else None,
        "events": valid,
    }


def stop_event_status(
    evidence: dict[str, Any] | None,
    callsign: str,
    run_id: str,
    actuator_records: list[dict[str, Any]],
) -> dict[str, Any]:
    """Validate authority-observed safe-stop and recovery events against the applier audit."""
    if not isinstance(evidence, dict) or not isinstance(evidence.get("stopEvents"), list):
        return {
            "status": "unavailable",
            "eventCount": 0,
            "transitionStops": [],
            "invalidReasons": [],
        }
    events = evidence["stopEvents"]
    if not events:
        mission_transitions = [
            event
            for event in evidence.get("missionProgress", [])
            if isinstance(event, dict) and event.get("event") == "waypoint_reached"
        ]
        if mission_transitions:
            return {
                "status": "measured",
                "eventCount": 0,
                "stopCount": 0,
                "transitionStops": [],
                "noTransitionStopsObserved": True,
                "transitionCount": len(mission_transitions),
                "invalidReasons": [],
            }
        return {
            "status": "unavailable",
            "eventCount": 0,
            "transitionStops": [],
            "invalidReasons": ["no-stop-events-or-mission-transition"],
        }
    valid: list[dict[str, Any]] = []
    invalid_reasons: list[str] = []
    last_sequence = 0
    last_timestamp = 0
    last_monotonic_timestamp = 0
    for event in events:
        if (
            not isinstance(event, dict)
            or event.get("runId") != run_id
            or event.get("callsign") != callsign
            or not isinstance(event.get("eventSequence"), int)
            or isinstance(event.get("eventSequence"), bool)
            or event["eventSequence"] <= last_sequence
            or not isinstance(event.get("atUnixMs"), int)
            or isinstance(event.get("atUnixMs"), bool)
            or event["atUnixMs"] < last_timestamp
            or event.get("clockSource") != "authority-process-monotonic"
            or not isinstance(event.get("atMonotonicMs"), int)
            or isinstance(event.get("atMonotonicMs"), bool)
            or event["atMonotonicMs"] < last_monotonic_timestamp
            or event.get("event") not in {"observed-stop", "recovered"}
        ):
            invalid_reasons.append("identity-sequence-or-timestamp-invalid")
            continue
        last_sequence = event["eventSequence"]
        last_timestamp = event["atUnixMs"]
        last_monotonic_timestamp = event["atMonotonicMs"]
        valid.append(event)
    stop_events = [event for event in valid if event.get("event") == "observed-stop"]
    recovery_events = [event for event in valid if event.get("event") == "recovered"]
    transition_stops = []
    known_classes = {
        "mission-setpoint-mismatch",
        "admission-missing-or-stale",
        "grant-expired",
        "boundary-denial",
        "other",
    }
    for stop in stop_events:
        reason_class = stop.get("reasonClass")
        if reason_class not in known_classes:
            invalid_reasons.append("stop-reason-class-invalid")
            continue
        if reason_class not in {"mission-setpoint-mismatch", "admission-missing-or-stale"}:
            continue
        binding = {
            "stopSequence": stop.get("stopSequence"),
            "stopCommandId": stop.get("stopCommandId"),
            "reasonClass": reason_class,
            "controllerDecision": stop.get("controllerDecision"),
            "grantSequence": stop.get("grantSequence"),
            "setpointSequence": stop.get("setpointSequence"),
            "vigieAdmissionSequence": stop.get("vigieAdmissionSequence"),
            "controllerHoldDurationMsAtPublication": stop.get(
                "controllerHoldDurationMsAtPublication"
            ),
            "authorityHoldStartedAtUnixMs": stop.get("atUnixMs"),
            "status": "measured",
        }
        if any(
            not isinstance(binding[key], int) or isinstance(binding[key], bool) or binding[key] < 0
            for key in (
                "stopSequence",
                "grantSequence",
                "setpointSequence",
                "vigieAdmissionSequence",
                "controllerHoldDurationMsAtPublication",
            )
        ):
            binding["status"] = "invalid"
            invalid_reasons.append("transition-stop-binding-incomplete")
        if not isinstance(binding["stopCommandId"], str) or not binding["stopCommandId"]:
            binding["status"] = "invalid"
            invalid_reasons.append("transition-stop-command-id-missing")
        if binding["controllerDecision"] not in {
            "publish-fail-closed-safe-stop",
            "publish-unconditional-actuator-neutralization",
        }:
            binding["status"] = "invalid"
            invalid_reasons.append("transition-stop-controller-decision-unsupported")
        recoveries = [
            event
            for event in recovery_events
            if event.get("stopSequence") == binding["stopSequence"]
        ]
        if len(recoveries) != 1:
            binding["status"] = "invalid"
            binding["recoveryStatus"] = "missing-or-ambiguous"
            invalid_reasons.append("transition-stop-recovery-missing-or-ambiguous")
            transition_stops.append(binding)
            continue
        recovery = recoveries[0]
        duration = recovery.get("holdDurationMs")
        recovery_sequence = recovery.get("recoveryActuatorSequence")
        recovery_grant = recovery.get("recoveryGrantSequence")
        hold_started = recovery.get("holdStartedAtMonotonicMs")
        if (
            not isinstance(duration, int)
            or isinstance(duration, bool)
            or duration < 0
            or duration > 2_000
            or not isinstance(recovery_sequence, int)
            or isinstance(recovery_sequence, bool)
            or not isinstance(recovery_grant, int)
            or isinstance(recovery_grant, bool)
            or recovery["atUnixMs"] < stop["atUnixMs"]
            or recovery["atUnixMs"] - stop["atUnixMs"] > 2_000
            or not isinstance(hold_started, int)
            or isinstance(hold_started, bool)
            or hold_started < 0
            or hold_started > stop["atMonotonicMs"]
            or hold_started != stop.get("actuatorHoldStartedAtMonotonicMs")
            or duration != recovery["atMonotonicMs"] - hold_started
        ):
            binding["status"] = "invalid"
            invalid_reasons.append("transition-stop-recovery-unbounded-or-unbound")
            transition_stops.append(binding)
            continue
        matching_applier = [
            record
            for record in actuator_records
            if record.get("stage") == "authorized-command"
            and record.get("authority") == "verified-core-grant"
            and record.get("runId") == run_id
            and record.get("vehicleId") == callsign
            and record.get("sequence") == recovery_sequence
            and isinstance(record.get("executionGrant"), dict)
            and record["executionGrant"].get("grantSequence") == recovery_grant
            and isinstance(record.get("recordedAtUnixMs"), int)
            and stop["atUnixMs"] - 250 <= record["recordedAtUnixMs"] <= recovery["atUnixMs"] + 500
        ]
        matching_stop_applier = [
            record
            for record in actuator_records
            if record.get("stage") == "explicit-stop"
            and record.get("authority") == "safe-stop"
            and record.get("neutral") is True
            and record.get("runId") == run_id
            and record.get("vehicleId") == callsign
            and record.get("commandId") == binding["stopCommandId"]
            and record.get("sequence") == binding["stopSequence"]
            and isinstance(record.get("recordedAtUnixMs"), int)
            and not isinstance(record.get("recordedAtUnixMs"), bool)
            and stop["atUnixMs"] - 250 <= record["recordedAtUnixMs"] <= recovery["atUnixMs"] + 500
        ]
        stop_recorded_at = min(
            (
                record["recordedAtUnixMs"]
                for record in matching_stop_applier
                if isinstance(record.get("recordedAtUnixMs"), int)
            ),
            default=None,
        )
        recovery_recorded_at = min(
            (
                record["recordedAtUnixMs"]
                for record in matching_applier
                if isinstance(record.get("recordedAtUnixMs"), int)
            ),
            default=None,
        )
        commands_during_hold = [
            record
            for record in actuator_records
            if record.get("stage") == "authorized-command"
            and record.get("runId") == run_id
            and record.get("vehicleId") == callsign
            and isinstance(record.get("recordedAtUnixMs"), int)
            and stop_recorded_at is not None
            and recovery_recorded_at is not None
            and stop_recorded_at < record["recordedAtUnixMs"] < recovery_recorded_at
            and record.get("sequence") != recovery_sequence
        ]
        binding["recoveryDurationMs"] = duration
        binding["durationClockSource"] = "authority-process-monotonic"
        binding["authorityHoldStartedAtMonotonicMs"] = hold_started
        binding["recoveryActuatorSequence"] = recovery_sequence
        binding["recoveryGrantSequence"] = recovery_grant
        binding["applierRecoveryCommandCount"] = len(matching_applier)
        binding["applierNeutralStopCommandCount"] = len(matching_stop_applier)
        binding["commandsDuringStopHold"] = len(commands_during_hold)
        if (
            not matching_applier
            or not matching_stop_applier
            or commands_during_hold
            or stop_recorded_at is None
            or recovery_recorded_at is None
            or stop_recorded_at > recovery_recorded_at
            or recovery_recorded_at - stop_recorded_at > 2_000
        ):
            binding["status"] = "invalid"
            invalid_reasons.append("transition-stop-applier-correlation-or-hold-integrity-failed")
        transition_stops.append(binding)
    mission_transitions = [
        event
        for event in evidence.get("missionProgress", [])
        if isinstance(event, dict) and event.get("event") == "waypoint_reached"
    ]
    if not transition_stops and not mission_transitions:
        invalid_reasons.append("no-transition-safe-stop-observed")
    return {
        "status": "invalid" if invalid_reasons else "measured",
        "eventCount": len(valid),
        "stopCount": len(stop_events),
        "transitionStops": transition_stops,
        "noTransitionStopsObserved": not transition_stops,
        "transitionCount": len(mission_transitions),
        "invalidReasons": sorted(set(invalid_reasons)),
        "eventSequenceValidated": len(valid) == len(events),
    }


def valid_vec3(value: Any) -> bool:
    return (
        isinstance(value, list)
        and len(value) == 3
        and all(
            isinstance(component, (int, float))
            and not isinstance(component, bool)
            and math.isfinite(component)
            for component in value
        )
    )


def cross_vehicle_effect_windows_valid(
    report: dict[str, Any], vehicles: list[dict[str, Any]], run_id: str
) -> bool:
    """Recompute per-vehicle deltas from independently captured probe windows."""
    snapshots = report.get("effectSnapshots")
    if isinstance(snapshots, dict):
        before = snapshots.get("beforeInjection") or snapshots.get("before")
        during = (
            snapshots.get("atControllerRejection")
            or snapshots.get("atControllerDecision")
            or snapshots.get("atDecision")
        )
        after = snapshots.get("afterOneSecond") or snapshots.get("afterObservation")
    else:
        before = report.get("effectSnapshotBefore")
        during = report.get("effectSnapshotAtDecision")
        after = report.get("effectSnapshotAfterObservation")
    if not all(isinstance(item, dict) for item in (before, during, after)):
        return False
    observed_times = [item.get("observedAtUnixMs") for item in (before, during, after)]
    sim_times = [item.get("poseSimulationTimeSeconds") for item in (before, during, after)]
    injection = report.get("injectionAtUnixMs")
    if (
        type(injection) is not int
        or any(type(value) is not int for value in observed_times)
        or not observed_times[0] <= injection <= observed_times[1] < observed_times[2]
        or observed_times[2] - injection > MAX_CROSS_VEHICLE_EFFECT_WINDOW_SECONDS * 1000
        or any(type(value) not in {int, float} or not math.isfinite(value) for value in sim_times)
        or not sim_times[0] <= sim_times[1] < sim_times[2]
        or sim_times[2] - sim_times[0] > MAX_CROSS_VEHICLE_EFFECT_WINDOW_SECONDS
    ):
        return False
    for snapshot in (before, during, after):
        poses = snapshot.get("poseByVehicle")
        actuators = snapshot.get("actuatorByVehicle")
        if not isinstance(poses, dict) or not isinstance(actuators, dict):
            return False
        if {item["model"] for item in vehicles} - set(
            value.get("model") for value in poses.values() if isinstance(value, dict)
        ):
            return False
        for vehicle in vehicles:
            callsign = vehicle["callsign"]
            actuator = actuators.get(callsign)
            if (
                not isinstance(actuator, dict)
                or actuator.get("runId") != run_id
                or actuator.get("vehicleId") != callsign
                or type(actuator.get("sequence")) is not int
            ):
                return False
            if actuator.get("stage") == "authorized-command" and (
                actuator.get("executionGrantRunId") != run_id
                or actuator.get("executionGrantVehicleId") != callsign
            ):
                return False
    tolerance = report.get("effectComparison", {}).get("poseEffectToleranceM")
    if (
        type(tolerance) not in {int, float}
        or not math.isfinite(tolerance)
        or tolerance != MAX_CROSS_VEHICLE_POSE_EFFECT_M
    ):
        return False
    before_poses = before["poseByVehicle"]
    during_poses = during["poseByVehicle"]
    after_poses = after["poseByVehicle"]
    for vehicle in vehicles:
        callsign, model = vehicle["callsign"], vehicle["model"]
        start = next((pose for pose in before_poses.values() if pose.get("model") == model), None)
        finish = next((pose for pose in after_poses.values() if pose.get("model") == model), None)
        decision_pose = next(
            (pose for pose in during_poses.values() if pose.get("model") == model), None
        )
        first_actuator = before["actuatorByVehicle"][callsign]
        decision_actuator = during["actuatorByVehicle"][callsign]
        last_actuator = after["actuatorByVehicle"][callsign]
        if (
            not isinstance(start, dict)
            or not isinstance(decision_pose, dict)
            or not isinstance(finish, dict)
            or not valid_vec3(start.get("positionEnuM"))
            or not valid_vec3(decision_pose.get("positionEnuM"))
            or not valid_vec3(finish.get("positionEnuM"))
            or type(start.get("simulationTimeSeconds")) not in {int, float}
            or type(decision_pose.get("simulationTimeSeconds")) not in {int, float}
            or type(finish.get("simulationTimeSeconds")) not in {int, float}
            or decision_pose["simulationTimeSeconds"] < start["simulationTimeSeconds"]
            or decision_pose["simulationTimeSeconds"] > finish["simulationTimeSeconds"]
            or finish["simulationTimeSeconds"] <= start["simulationTimeSeconds"]
            or math.dist(start["positionEnuM"], decision_pose["positionEnuM"]) > tolerance
            or math.dist(start["positionEnuM"], finish["positionEnuM"]) > tolerance
            or decision_actuator["sequence"] < first_actuator["sequence"]
            or last_actuator["sequence"] < first_actuator["sequence"]
            or last_actuator["sequence"] < decision_actuator["sequence"]
        ):
            return False
    return True


def selected_vehicle_authorization_evidence(
    run_dir: Path, plan: dict[str, Any], vehicles: list[dict[str, Any]], run_id: str
) -> dict[str, Any] | None:
    """Correlate one selected vehicle's extra waypoint with grant, actuator, pose, and peer hold."""
    selection = plan.get("selectedVehicleAuthorization")
    if not isinstance(selection, dict):
        return None
    callsign = selection.get("callsign")
    previous = selection.get("previousWaypointEnuM")
    command = selection.get("commandWaypointEnuM")
    vehicle = next((item for item in vehicles if item.get("callsign") == callsign), None)
    if vehicle is None or not valid_vec3(previous) or not valid_vec3(command):
        return {"status": "inconclusive", "reason": "selected-command-plan-invalid"}
    evidence_path = run_dir / f"flight-evidence-{callsign}.json"
    try:
        evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
        progress = evidence.get("missionProgress", [])
    except (OSError, json.JSONDecodeError):
        return {"status": "inconclusive", "reason": "selected-command-progress-unavailable"}
    target_grants = [
        event
        for event in progress
        if isinstance(event, dict)
        and event.get("event") == "grant_issued"
        and event.get("runId") == run_id
        and event.get("callsign") == callsign
        and event.get("targetEnuM") == command
    ]
    if len(target_grants) != 1:
        return {
            "status": "inconclusive",
            "reason": "selected-command-grant-not-unique",
            "grantCount": len(target_grants),
        }
    grant = target_grants[0]
    plan_vehicle = next(
        (item for item in plan.get("vehicles", []) if item.get("callsign") == callsign), None
    )
    if not isinstance(plan_vehicle, dict):
        return {"status": "inconclusive", "reason": "selected-command-route-missing"}
    target_arrivals = [
        event
        for event in progress
        if isinstance(event, dict)
        and event.get("event") == "waypoint_reached"
        and event.get("runId") == run_id
        and event.get("callsign") == callsign
        and event.get("advancingToWaypointIndex") == len(plan_vehicle.get("waypointsEnuM", [])) - 1
    ]
    if len(target_arrivals) != 1 or not isinstance(target_arrivals[0].get("simulationTimeNs"), int):
        return {"status": "inconclusive", "reason": "selected-command-arrival-not-unique"}
    arrival_time = target_arrivals[0]["simulationTimeNs"] / 1e9
    actuator_rows = []
    try:
        with (run_dir / "applier-neutral" / "actuator-output.jsonl").open(
            encoding="utf-8"
        ) as stream:
            for line in stream:
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                signed = row.get("executionGrant") or {}
                if (
                    row.get("vehicleId") == callsign
                    and row.get("runId") == run_id
                    and row.get("stage") == "authorized-command"
                    and signed.get("runId") == run_id
                    and signed.get("vehicleId") == callsign
                    and signed.get("targetEnuMm") == [round(value * 1000) for value in command]
                    and signed.get("grantSequence") == grant.get("grantSequence")
                ):
                    actuator_rows.append(row)
    except OSError:
        pass
    if not actuator_rows:
        return {"status": "inconclusive", "reason": "selected-command-actuator-grant-not-found"}
    pose_samples = load_pose_samples(run_dir, {item["model"] for item in vehicles})
    target_model = vehicle["model"]
    target_samples = pose_samples.get(target_model, [])
    peer_samples = [item for item in vehicles if item["callsign"] != callsign]
    peer = peer_samples[0] if len(peer_samples) == 1 else None
    if not peer:
        return {"status": "inconclusive", "reason": "positive-control-requires-one-peer"}
    peer_plan = next(
        (item for item in plan.get("vehicles", []) if item.get("callsign") == peer["callsign"]),
        None,
    )
    peer_path = peer_plan.get("finalSlotEnuM") if isinstance(peer_plan, dict) else None
    if not valid_vec3(peer_path):
        return {"status": "inconclusive", "reason": "peer-final-waypoint-unavailable"}
    peer_rows = pose_samples.get(peer["model"], [])
    if (
        len(target_samples) < MINIMUM_POSE_SAMPLES_PER_VEHICLE
        or len(peer_rows) < MINIMUM_POSE_SAMPLES_PER_VEHICLE
    ):
        return {"status": "inconclusive", "reason": "selected-command-pose-window-incomplete"}
    proof_path = run_dir / f"gz-flight-proof-report-{callsign}.json"
    try:
        proof = json.loads(proof_path.read_text(encoding="utf-8"))
        final_leg = proof.get("measured", {}).get("legs", [])[-1]
        origin = proof.get("timingReference", {}).get("simulationTimeOriginSeconds")
        evaluator_arrival_time = origin + final_leg.get("simSeconds")
        evaluator_skew = abs(evaluator_arrival_time - arrival_time)
    except (OSError, json.JSONDecodeError, TypeError, KeyError, IndexError):
        evaluator_arrival_time = None
    # The evaluator's world pose stream owns simulation time. The authority
    # reports its own clock, which can run at a different rate; correlate the
    # authorization to the independent evaluator arrival using the signed
    # grant's wall-time interval and the matching actuator grant sequence.
    grant_at_unix_ms = grant.get("atUnixMs")
    actuator_at_unix_ms = min(
        (
            row.get("recordedAtUnixMs")
            for row in actuator_rows
            if type(row.get("recordedAtUnixMs")) is int
        ),
        default=None,
    )
    if (
        evaluator_arrival_time is None
        or type(grant_at_unix_ms) is not int
        or type(actuator_at_unix_ms) is not int
        or abs(actuator_at_unix_ms - grant_at_unix_ms) > 2_000
    ):
        return {
            "status": "inconclusive",
            "reason": "selected-command-wall-time-correlation-unavailable",
            "runId": run_id,
            "callsign": callsign,
            "authorityArrivalSimulationTimeSeconds": round(arrival_time, 6),
            "independentEvaluatorArrivalSimulationTimeSeconds": round(evaluator_arrival_time, 6)
            if evaluator_arrival_time is not None
            else None,
            "simulationClockSkewSeconds": round(evaluator_skew, 6)
            if evaluator_skew is not None
            else None,
            "grantAtUnixMs": grant_at_unix_ms,
            "actuatorAtUnixMs": actuator_at_unix_ms,
            "grantSequence": grant.get("grantSequence"),
            "actuatorCommandCount": len(actuator_rows),
            "poseSource": "gazebo-pose.jsonl",
            "grantSource": f"flight-evidence-{callsign}.json#missionProgress",
        }
    arrival_time = evaluator_arrival_time
    target_pose = (
        min(target_samples, key=lambda sample: abs(sample[0] - arrival_time))
        if target_samples
        else None
    )
    target_final = list(target_pose[1:]) if target_pose else None
    peer_pose = (
        min(peer_rows, key=lambda sample: abs(sample[0] - arrival_time)) if peer_rows else None
    )
    peer_final = list(peer_pose[1:]) if peer_pose else None
    target_pose_skew = abs(target_pose[0] - arrival_time) if target_pose else None
    peer_pose_skew = abs(peer_pose[0] - arrival_time) if peer_pose else None
    target_error = math.dist(target_final, command) if target_final else None
    peer_error = math.dist(peer_final, peer_path) if peer_final else None
    target_before = [sample for sample in target_samples if sample[0] < arrival_time - 1.0]
    peer_before = [sample for sample in peer_rows if sample[0] < arrival_time - 1.0]
    target_start = list(target_before[-1][1:]) if target_before else None
    peer_start = list(peer_before[-1][1:]) if peer_before else None
    target_displacement = (
        math.dist(target_start, target_final) if target_start and target_final else None
    )
    target_route_progress = (
        math.dist(previous, target_final) if target_final and valid_vec3(previous) else None
    )
    peer_displacement = math.dist(peer_start, peer_final) if peer_start and peer_final else None
    passed = (
        target_error is not None
        and peer_error is not None
        and target_displacement is not None
        and peer_displacement is not None
        and target_pose_skew is not None
        and peer_pose_skew is not None
        and target_pose_skew <= 1.0
        and peer_pose_skew <= 1.0
        and target_error <= 1.5
        and peer_error <= 1.5
        and target_route_progress is not None
        and target_route_progress >= 1.0
        and peer_displacement <= 1.0
    )
    return {
        "status": "passed"
        if passed
        else "failed"
        if target_error is not None and peer_error is not None
        else "inconclusive",
        "runId": run_id,
        "callsign": callsign,
        "peerCallsign": peer["callsign"],
        "authorizationPath": selection.get("authorizationPath"),
        "previousWaypointEnuM": previous,
        "commandWaypointEnuM": command,
        "grantSequence": grant.get("grantSequence"),
        "grantAtUnixMs": grant.get("atUnixMs"),
        "firstActuatorAtUnixMs": actuator_at_unix_ms,
        "arrivalSimulationTimeSeconds": round(arrival_time, 6),
        "authorityArrivalSimulationTimeSeconds": round(
            target_arrivals[0]["simulationTimeNs"] / 1e9, 6
        ),
        "simulationClockSkewSeconds": round(evaluator_skew, 6)
        if evaluator_skew is not None
        else None,
        "actuatorCommandCount": len(actuator_rows),
        "firstActuatorSequence": min(row.get("sequence", 0) for row in actuator_rows),
        "targetFinalPoseEnuM": target_final,
        "targetPoseTimeSkewSeconds": round(target_pose_skew, 6)
        if target_pose_skew is not None
        else None,
        "targetFinalErrorM": round(target_error, 4) if target_error is not None else None,
        "targetOneSecondPriorPoseEnuM": target_start,
        "targetWindowDisplacementM": round(target_displacement, 4)
        if target_displacement is not None
        else None,
        "targetRouteProgressFromPreviousWaypointM": round(target_route_progress, 4)
        if target_route_progress is not None
        else None,
        "peerPlannedFinalEnuM": peer_path,
        "peerFinalPoseEnuM": peer_final,
        "peerPoseTimeSkewSeconds": round(peer_pose_skew, 6) if peer_pose_skew is not None else None,
        "peerFinalErrorM": round(peer_error, 4) if peer_error is not None else None,
        "peerOneSecondPriorPoseEnuM": peer_start,
        "peerWindowDisplacementM": round(peer_displacement, 4)
        if peer_displacement is not None
        else None,
        "toleranceM": 1.5,
        "poseSource": "gazebo-pose.jsonl",
        "grantSource": f"flight-evidence-{callsign}.json#missionProgress",
        "actuatorSource": "applier-neutral/actuator-output.jsonl",
    }


def load_actuator_records(run_dir: Path) -> dict[str, list[dict[str, Any]]]:
    records: dict[str, list[dict[str, Any]]] = {}
    path = run_dir / "applier-neutral" / "actuator-output.jsonl"
    try:
        with path.open(encoding="utf-8") as stream:
            for line in stream:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(record, dict):
                    continue
                callsign = record.get("vehicleId")
                if isinstance(callsign, str):
                    records.setdefault(callsign, []).append(record)
    except OSError:
        pass
    return records


def load_controller_outcomes(run_dir: Path, callsign: str) -> list[dict[str, Any]]:
    path = run_dir / "logs" / f"antoine-controller-{callsign}.log"
    outcomes = []
    try:
        with path.open(encoding="utf-8") as stream:
            for line_number, line in enumerate(stream, start=1):
                if not line.startswith("ANTOINE_GZ_VETO_RESULT "):
                    continue
                fields = {}
                for token in line.split()[1:]:
                    key, separator, value = token.partition("=")
                    if separator:
                        fields[key] = value
                if fields.get("callsign") != callsign:
                    continue
                try:
                    vigie_sequence = int(fields["vigie_sequence"])
                except (KeyError, ValueError):
                    continue
                outcomes.append(
                    {
                        "lineNumber": line_number,
                        "vigieSequence": vigie_sequence,
                        "decision": fields.get("decision"),
                        "vigieCorrelation": fields.get("vigie_correlation"),
                    }
                )
    except OSError:
        pass
    return outcomes


def execution_authority_status(
    report: dict[str, Any], run_id: str, callsign: str
) -> dict[str, Any]:
    """Read Antoine's grant state without treating it as vehicle arming state."""
    telemetry = report.get("telemetry")
    if not isinstance(telemetry, list) or not telemetry:
        return {
            "status": "unavailable",
            "reason": "No controller authority telemetry was recorded.",
        }
    if any(
        not isinstance(sample, dict)
        or sample.get("runId") != run_id
        or sample.get("identity") != callsign
        or sample.get("source") != "antoine-gz-plant"
        or not isinstance(sample.get("observedAtUnixMs"), int)
        or isinstance(sample.get("observedAtUnixMs"), bool)
        or not isinstance(sample.get("executionGrantActive"), bool)
        for sample in telemetry
    ):
        return {
            "status": "invalid",
            "reason": "Telemetry is malformed or does not match the run and callsign.",
        }
    latest = max(telemetry, key=lambda sample: sample["observedAtUnixMs"])
    if sum(sample["observedAtUnixMs"] == latest["observedAtUnixMs"] for sample in telemetry) != 1:
        return {
            "status": "invalid",
            "reason": "Latest execution-authority telemetry timestamp is ambiguous.",
        }
    expected_authority = "core-execution-grant" if latest["executionGrantActive"] else "safe-hold"
    if latest.get("control_authority") != expected_authority:
        return {
            "status": "invalid",
            "reason": "executionGrantActive conflicts with the controller authority label.",
        }
    return {
        "status": "measured",
        "executionGrantActive": latest["executionGrantActive"],
        "controlAuthority": latest["control_authority"],
        "observedAtUnixMs": latest["observedAtUnixMs"],
        "source": latest["source"],
    }


def controller_state_cadence_status(
    report: dict[str, Any], run_id: str, callsign: str
) -> dict[str, Any]:
    telemetry = report.get("telemetry")
    if not isinstance(telemetry, list) or len(telemetry) < MINIMUM_CADENCE_TELEMETRY_SAMPLES:
        return {"status": "unavailable", "reason": "insufficient-run-bound-telemetry"}
    valid = []
    for sample in telemetry:
        if (
            not isinstance(sample, dict)
            or sample.get("runId") != run_id
            or sample.get("identity") != callsign
            or sample.get("source") != "antoine-gz-plant"
            or not isinstance(sample.get("observedAtUnixMs"), int)
            or isinstance(sample.get("observedAtUnixMs"), bool)
        ):
            return {"status": "invalid", "reason": "telemetry-identity-or-timestamp-mismatch"}
        control_hz = sample.get("configuredControlRateHz")
        state_hz = sample.get("observedStateRateHz")
        ticks_per_sample = sample.get("ticksPerStateSample")
        fresh_samples = sample.get("stateSamplesObserved")
        if (
            any(
                not isinstance(value, (int, float))
                or isinstance(value, bool)
                or not math.isfinite(value)
                for value in (control_hz, state_hz, ticks_per_sample)
            )
            or control_hz <= 0
            or state_hz < 0
            or ticks_per_sample <= 0
            or not isinstance(fresh_samples, int)
            or isinstance(fresh_samples, bool)
            or fresh_samples < 0
        ):
            return {"status": "invalid", "reason": "invalid-controller-state-rate-fields"}
        valid.append(sample)
    timestamps = [sample["observedAtUnixMs"] for sample in valid]
    if any(later <= earlier for earlier, later in itertools.pairwise(timestamps)):
        return {"status": "invalid", "reason": "telemetry-timestamps-not-ordered"}
    fresh_counts = [sample["stateSamplesObserved"] for sample in valid]
    if any(later < earlier for earlier, later in itertools.pairwise(fresh_counts)):
        return {"status": "invalid", "reason": "state-sample-counter-regressed"}
    control_rates = {float(sample["configuredControlRateHz"]) for sample in valid}
    if len(control_rates) != 1:
        return {"status": "invalid", "reason": "configured-control-rate-changed"}
    for sample in valid:
        expected_state_hz = sample["configuredControlRateHz"] / sample["ticksPerStateSample"]
        tolerance_hz = max(0.5, expected_state_hz * 0.1)
        if abs(sample["observedStateRateHz"] - expected_state_hz) > tolerance_hz:
            return {"status": "invalid", "reason": "state-rate-conflicts-with-sample-ratio"}
    latest = valid[-1]
    if latest["stateSamplesObserved"] < 64:
        return {"status": "unavailable", "reason": "rate-estimator-not-warmed"}
    ticks_per_sample = float(latest["ticksPerStateSample"])
    passed = ticks_per_sample <= MAXIMUM_CONTROL_TICKS_PER_STATE_SAMPLE
    return {
        "status": "passed" if passed else "failed",
        "configuredControlRateHz": float(latest["configuredControlRateHz"]),
        "observedStateRateHz": float(latest["observedStateRateHz"]),
        "ticksPerStateSample": ticks_per_sample,
        "controlToStateRateRatio": ticks_per_sample,
        "rateEstimatorFreshStateSamples": latest["stateSamplesObserved"],
        "telemetrySampleCount": len(valid),
        "firstObservedAtUnixMs": timestamps[0],
        "lastObservedAtUnixMs": timestamps[-1],
        "maximumControlTicksPerStateSample": MAXIMUM_CONTROL_TICKS_PER_STATE_SAMPLE,
    }


def load_provenance(run_dir: Path) -> tuple[bool | None, dict[str, Any]]:
    try:
        comparison = json.loads(
            (run_dir / "research-provenance-comparison.json").read_text(encoding="utf-8")
        )
        provenance = json.loads((run_dir / "research-provenance.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None, {"status": "unavailable"}
    if not isinstance(comparison, dict) or not isinstance(provenance, dict):
        return False, {"status": "invalid"}
    stable = (
        isinstance(comparison, dict)
        and comparison.get("runId") == run_dir.name
        and comparison.get("prePhaseValid") is True
        and comparison.get("postPhaseValid") is True
        and comparison.get("stable") is True
    )
    trees = {
        item.get("name"): {
            "available": item.get("available"),
            "revision": item.get("revision"),
            "worktreeDirty": item.get("worktreeDirty"),
            "worktreeStatusSha256": item.get("worktreeStatusSha256"),
            "sourceContentSha256": item.get("sourceContentSha256"),
        }
        for item in provenance.get("sourceTrees", [])
        if isinstance(item, dict)
        and item.get("name")
        in {"antoine", "gazebo_distribution", "gazebo_sim_assets", "dvd_research"}
    }
    inputs = {
        item.get("name"): {"available": item.get("available"), "sha256": item.get("sha256")}
        for item in provenance.get("inputs", [])
        if isinstance(item, dict)
        and item.get("name")
        in {
            "gazebo_binary",
            "source_world",
            "runtime_world_overlay",
            "x500_model",
            "antoine_sitl_binary",
            "antoine_local_authority_binary",
            "antoine_boundary_binary",
            "gazebo_applier",
            "gazebo_model_assets",
            "gazebo_source_snapshot_receipt",
            "antoine_build_source_snapshot",
            "antoine_source_snapshot_receipt",
            "antoine_build_receipt",
        }
    }
    return stable, {
        "status": "stable" if stable else "unstable",
        "sourceTrees": trees,
        "executables": inputs,
    }


def hash_run_evidence(run_dir: Path, relative_paths: list[str]) -> dict[str, dict[str, Any]]:
    evidence: dict[str, dict[str, Any]] = {}
    for relative in relative_paths:
        path = run_dir / relative
        if path.is_symlink() or not path.is_file():
            evidence[relative] = {"available": False}
            continue
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        evidence[relative] = {
            "available": True,
            "sizeBytes": path.stat().st_size,
            "sha256": digest.hexdigest(),
        }
    return evidence


def validate_swarm_report(report: dict[str, Any], run_id: str) -> None:
    required = {
        "contract",
        "version",
        "runId",
        "status",
        "behaviorStatus",
        "probeStatus",
        "baselineStatus",
        "baselineGates",
        "vehicleCount",
        "vehicles",
        "vehiclePairs",
        "poseStreamQualityPassed",
        "poseStreamQuality",
        "evidenceArtifacts",
        "controllerStateCadenceQualified",
    }
    if not required.issubset(report):
        raise ValueError("generated swarm report is missing required fields")
    if (
        report["contract"] != "damn-vulnerable-drone.antoine-swarm-flight"
        or report["version"] != SWARM_REPORT_VERSION
        or report["runId"] != run_id
        or report["status"] not in {"passed", "failed", "inconclusive"}
        or report["behaviorStatus"] not in {"passed", "failed", "inconclusive"}
        or report["probeStatus"] not in {"passed", "failed", "inconclusive", "not_requested"}
        or report["baselineStatus"] not in {"qualified_baseline", "inconclusive"}
    ):
        raise ValueError("generated swarm report has invalid contract identity or status")
    vehicles = report["vehicles"]
    pairs = report["vehiclePairs"]
    if (
        not isinstance(vehicles, dict)
        or not 2 <= len(vehicles) <= 5
        or report["vehicleCount"] != len(vehicles)
        or not isinstance(pairs, dict)
        or len(pairs) != len(vehicles) * (len(vehicles) - 1) // 2
    ):
        raise ValueError("generated swarm report has an invalid vehicle or pair roster")
    for callsign, vehicle in vehicles.items():
        if (
            not isinstance(vehicle, dict)
            or vehicle.get("controllerIdentity", {}).get("callsign") != callsign
            or vehicle.get("controllerIdentity", {}).get("runId") != run_id
            or not isinstance(vehicle.get("poseStreamQuality"), dict)
            or not isinstance(vehicle.get("actuatorEvidence"), dict)
            or vehicle.get("controllerStateCadence", {}).get("status")
            not in {"passed", "failed", "unavailable", "invalid"}
            or vehicle["actuatorEvidence"].get("finalDisarmed", {}).get("status")
            not in {"measured", "unavailable"}
        ):
            raise ValueError(f"generated swarm report has invalid vehicle evidence for {callsign}")
    for pair in pairs.values():
        if (
            not isinstance(pair, dict)
            or pair.get("measurementStatus") not in {"measured", "inconclusive"}
            or (
                pair.get("minimumSeparationM") is not None
                and not math.isfinite(pair["minimumSeparationM"])
            )
        ):
            raise ValueError("generated swarm report has invalid pairwise evidence")
    if (
        not isinstance(report["baselineGates"], dict)
        or any(type(value) is not bool for value in report["baselineGates"].values())
        or not isinstance(report["evidenceArtifacts"], dict)
    ):
        raise ValueError("generated swarm report has invalid baseline or artifact gates")
    for name, artifact in report["evidenceArtifacts"].items():
        if not isinstance(artifact, dict) or type(artifact.get("available")) is not bool:
            raise ValueError(f"generated swarm report has invalid artifact state: {name}")
        if artifact["available"] and (
            not isinstance(artifact.get("sizeBytes"), int)
            or not isinstance(artifact.get("sha256"), str)
            or len(artifact["sha256"]) != 64
        ):
            raise ValueError(f"generated swarm report has invalid artifact digest: {name}")
    if report["baselineStatus"] == "qualified_baseline" and not all(
        report["baselineGates"].values()
    ):
        raise ValueError("generated swarm report qualified a baseline with a failed gate")
    if report["status"] == "passed" and (
        report["baselineStatus"] != "qualified_baseline"
        or report["behaviorStatus"] != "passed"
        or report["probeStatus"] not in {"passed", "not_requested"}
    ):
        raise ValueError("generated swarm report passed without complete qualification")


def main() -> int:
    if len(sys.argv) != 2:
        raise SystemExit("usage: summarize_antoine_two_drone_flight.py RUN_DIR")
    run_dir = Path(sys.argv[1]).resolve()
    try:
        config = load_swarm_config(run_dir)
        vehicles = config["vehicles"]
        plan = load_swarm_plan(run_dir, config, vehicles)
    except ValueError as error:
        raise SystemExit(str(error)) from error
    pose_samples = load_pose_samples(run_dir, {item["model"] for item in vehicles})
    pose_stream_quality = {
        model: measure_pose_stream(samples) for model, samples in pose_samples.items()
    }
    all_pose_streams_passed = all(item["passed"] for item in pose_stream_quality.values())
    evidence_artifacts = hash_run_evidence(
        run_dir,
        [
            "gazebo-pose.jsonl",
            "applier-neutral/actuator-output.jsonl",
            "applier-neutral/actuator-lifecycle.jsonl",
            *[f"applier-neutral/{item['callsign']}.armed-state.json" for item in vehicles],
            "research-swarm-plan.json",
            "research-swarm-config.json",
            "research-provenance-comparison.json",
            "research-cross-vehicle-authorization.json",
            "research-swarm-rate-disruption.json",
            "research-gazebo-source-snapshot.json",
            "research-antoine-build-receipt.json",
            "research-antoine-process-launches.tsv",
            "gazebo-source-snapshot/gazebo-source-snapshot.json",
            "antoine-source-snapshot/antoine-source-snapshot.json",
            *[
                path.relative_to(run_dir).as_posix()
                for snapshot_dir in (
                    run_dir / "gazebo-source-snapshot",
                    run_dir / "antoine-source-snapshot",
                    run_dir / "antoine-binaries",
                )
                if snapshot_dir.is_dir() and not snapshot_dir.is_symlink()
                for path in snapshot_dir.rglob("*")
                if path.is_file() and not path.is_symlink()
            ],
            *[f"flight-evidence-{item['callsign']}.json" for item in vehicles],
            *[f"gz-flight-proof-report-{item['callsign']}.json" for item in vehicles],
            *[
                path.relative_to(run_dir).as_posix()
                for path in (run_dir / "logs").glob("local-authority-drone-*.log")
            ],
            *[
                path.relative_to(run_dir).as_posix()
                for path in (run_dir / "logs").glob("antoine-controller-drone-*.log")
            ],
        ],
    )
    trajectory_available = write_trajectory_svg(run_dir, vehicles, pose_samples)
    poses_by_callsign = {
        vehicle["callsign"]: pose_samples[vehicle["model"]] for vehicle in vehicles
    }
    flight_reports = {
        vehicle["callsign"]: load_flight_report(run_dir, vehicle["callsign"])
        for vehicle in vehicles
    }
    actuator_records_by_callsign = load_actuator_records(run_dir)
    formation_samples, formation_time = synchronized_waypoint_snapshot(
        vehicles, poses_by_callsign, flight_reports
    )
    provenance_stable, provenance_summary = load_provenance(run_dir)
    provenance_trees = provenance_summary.get("sourceTrees", {})
    provenance_inputs = provenance_summary.get("executables", {})
    behavior_evaluation = evaluate_plan(
        plan,
        formation_samples or poses_by_callsign,
        min_separation_m=MINIMUM_SEPARATION_METERS,
    )
    behavior_evaluation["evaluationTimeSeconds"] = formation_time
    behavior_evaluation["evaluationBasis"] = (
        "common Gazebo simulation timestamp when each vehicle had reached its final planned waypoint"
        if formation_time is not None
        else "inconclusive: final-waypoint timing or synchronized pose evidence unavailable"
    )
    if formation_time is None:
        behavior_evaluation["status"] = "inconclusive"
        behavior_evaluation["gates"]["synchronizedFinalWaypointTimeAvailable"] = False
    vehicle_results = {}
    for vehicle in vehicles:
        callsign, model = vehicle["callsign"], vehicle["model"]
        report = flight_reports[callsign]
        raw_evidence_path = run_dir / f"flight-evidence-{callsign}.json"
        try:
            raw_evidence = json.loads(raw_evidence_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            raw_evidence = None
        grant = report.get("localGrant") or {}
        measured = report.get("measured") or {}
        identity_matches = grant.get("runId") == run_dir.name and grant.get("vehicleId") == callsign
        actuator_measurements = {
            key: measured.get(key)
            for key in (
                "signedActuatorCommands",
                "stopCommands",
                "applierNeutralRows",
                "lastMotorCommandRadS",
            )
            if key in measured
        }
        waypoint_reports = measured.get("legs", [])
        mission_progress = mission_progress_status(
            raw_evidence if isinstance(raw_evidence, dict) else None,
            callsign,
            run_dir.name,
            waypoint_reports if isinstance(waypoint_reports, list) else [],
            actuator_records_by_callsign.get(callsign, []),
            load_controller_outcomes(run_dir, callsign),
        )
        safe_stop_evidence = stop_event_status(
            raw_evidence if isinstance(raw_evidence, dict) else None,
            callsign,
            run_dir.name,
            actuator_records_by_callsign.get(callsign, []),
        )
        unreached = [
            item.get("index")
            for item in waypoint_reports
            if isinstance(item, dict) and item.get("reached") is not True
        ]
        kinematic_failures = [
            item.get("index")
            for item in waypoint_reports
            if isinstance(item, dict) and item.get("kinematicArrivalPassed") is False
        ]
        missing_progress = mission_progress["missingEvaluatorTransitions"]
        invalid_progress = mission_progress["invalidTransitionWaypointIndices"]
        last_progress = mission_progress.get("lastProgress")
        first_progress_failure = (
            missing_progress[0]
            if missing_progress
            else invalid_progress[0]
            if invalid_progress
            else None
        )
        first_invalid_reason = (
            mission_progress["invalidReasons"][0]
            if mission_progress["invalidReasons"]
            else "unspecified-validation-error"
        )
        progress_status = mission_progress["status"]
        progress_failure_status = (
            "failed"
            if unreached
            or kinematic_failures
            or missing_progress
            or invalid_progress
            or progress_status == "invalid"
            else "unavailable"
            if progress_status == "unavailable"
            else "none"
        )
        vehicle_results[callsign] = {
            "model": model,
            "runIdentityMatches": identity_matches,
            "allWaypointsReached": measured.get("allLegsReached") is True,
            "waypoints": waypoint_reports,
            "missionProgress": mission_progress,
            "safeStopEvidence": safe_stop_evidence,
            "missionProgressFailure": {
                "status": progress_failure_status,
                "firstUnreachedWaypointIndex": unreached[0] if unreached else None,
                "firstKinematicallyUnqualifiedWaypointIndex": kinematic_failures[0]
                if kinematic_failures
                else None,
                "firstUnrecordedTransitionWaypointIndex": (first_progress_failure),
                "lastAuthorityProgress": last_progress,
                "explanation": (
                    f"evaluator did not observe arrival at waypoint {unreached[0]}"
                    if unreached
                    else f"waypoint {kinematic_failures[0]} was reached positionally but continuous kinematic dwell did not pass"
                    if kinematic_failures
                    else f"authority did not record transition after waypoint {missing_progress[0]}"
                    if missing_progress
                    else (
                        f"mission progress transition {invalid_progress[0]} failed validation: "
                        f"{', '.join(mission_progress['invalidReasons'])}"
                        if invalid_progress
                        else f"mission progress failed validation: {first_invalid_reason}"
                    )
                    if progress_status == "invalid"
                    else "mission progress evidence is unavailable"
                    if progress_status == "unavailable"
                    else None
                ),
            },
            "maxHorizontalTravelM": measured.get("maxHorizontalTravelM"),
            "peakAltitudeM": measured.get("peakAltitudeM"),
            "independentPosePassed": report.get("poseTimelinePassed") is True,
            "actuatorNeutralizationPassed": report.get("actuatorNeutralizationPassed") is True,
            "localFlightPassed": report.get("localFlightPassed") is True,
            "poseSamples": len(pose_samples[model]),
            "postNeutralizationFinalPositionEnuM": (
                list(pose_samples[model][-1][1:]) if pose_samples[model] else None
            ),
            "poseStreamQuality": pose_stream_quality[model],
            "authorityScope": report.get("authorityScope"),
            "finalExecutionAuthority": execution_authority_status(report, run_dir.name, callsign),
            "controllerStateCadence": controller_state_cadence_status(
                report, run_dir.name, callsign
            ),
            "controllerIdentity": {
                "process": "antoine_sitl_nats",
                "callsign": callsign,
                "runId": run_dir.name,
                "sourceRevision": provenance_trees.get("antoine", {}).get("revision"),
                "binarySha256": provenance_inputs.get("antoine_sitl_binary", {}).get("sha256"),
            },
            "actuatorEvidence": {
                "neutralizationPassed": report.get("actuatorNeutralizationPassed") is True,
                "measurements": actuator_measurements,
                "finalDisarmed": disarmed_status(
                    run_dir, callsign, raw_evidence, actuator_records_by_callsign.get(callsign, [])
                ),
            },
        }
    progress_document = {
        "contract": "damn-vulnerable-drone.antoine-mission-progress",
        "version": "1.0.0",
        "runId": run_dir.name,
        "vehicles": {
            callsign: {
                "status": item["missionProgress"]["status"],
                "waypointTransitions": item["missionProgress"]["waypointTransitions"],
                "transitionBindings": item["missionProgress"]["transitionBindings"],
                "evaluatorReachedWaypoints": item["missionProgress"]["evaluatorReachedWaypoints"],
                "unconfirmedTransitions": item["missionProgress"]["unconfirmedTransitions"],
                "missingEvaluatorTransitions": item["missionProgress"][
                    "missingEvaluatorTransitions"
                ],
                "invalidReasons": item["missionProgress"]["invalidReasons"],
                "missionProgressFailure": item["missionProgressFailure"],
                "safeStopEvidence": item["safeStopEvidence"],
                "events": item["missionProgress"]["events"],
            }
            for callsign, item in vehicle_results.items()
        },
    }
    (run_dir / "research-antoine-progress.json").write_text(
        json.dumps(progress_document, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    pair_results = {}
    for first, second in itertools.combinations(vehicles, 2):
        pair_key = f"{first['callsign']}/{second['callsign']}"
        measurement = measure_pair(pose_samples[first["model"]], pose_samples[second["model"]])
        pair_results[pair_key] = {
            "firstModel": first["model"],
            "secondModel": second["model"],
            **measurement,
        }
    all_flights_passed = all(
        item["runIdentityMatches"]
        and item["allWaypointsReached"]
        and item["independentPosePassed"]
        and item["actuatorNeutralizationPassed"]
        and item["localFlightPassed"]
        and bool(item["waypoints"])
        and all(
            isinstance(leg, dict)
            and leg.get("reached") is True
            and leg.get("kinematicArrivalPassed") is True
            for leg in item["waypoints"]
        )
        and item["missionProgress"].get("status") == "measured"
        and not item["missionProgress"].get("invalidReasons")
        for item in vehicle_results.values()
    )
    all_actuator_evidence_passed = all(
        item["actuatorEvidence"]["neutralizationPassed"]
        and "stopCommands" in item["actuatorEvidence"]["measurements"]
        and "applierNeutralRows" in item["actuatorEvidence"]["measurements"]
        for item in vehicle_results.values()
    )
    independent_artifacts_present = all(
        evidence_artifacts.get(name, {}).get("available") is True
        for name in (
            "gazebo-pose.jsonl",
            "applier-neutral/actuator-output.jsonl",
            *[f"gz-flight-proof-report-{item['callsign']}.json" for item in vehicles],
        )
    )
    all_pairs_measured = bool(pair_results) and all(
        item["measurementStatus"] == "measured" for item in pair_results.values()
    )
    all_pairs_separated = all_pairs_measured and all(
        item["minimumSeparationM"] >= MINIMUM_SEPARATION_METERS for item in pair_results.values()
    )
    pair_count = len(pair_results)
    fault_path = run_dir / "research-swarm-fault.json"
    try:
        fault_observation = json.loads(fault_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        fault_observation = None
    rate_probe_requested = config.get("fault") == "command-rate"
    rate_probe_path = run_dir / "research-swarm-rate-disruption.json"
    try:
        rate_probe_observation = json.loads(rate_probe_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        rate_probe_observation = None
    if (
        not isinstance(rate_probe_observation, dict)
        or rate_probe_observation.get("contract")
        != "damn-vulnerable-drone.antoine-setpoint-rate-probe"
        or rate_probe_observation.get("runId") != run_dir.name
        or rate_probe_observation.get("status") not in {"passed", "failed", "inconclusive"}
    ):
        rate_probe_observation = None
    mavlink_path = run_dir / "research-mavlink-swarm.json"
    try:
        mavlink_observation = json.loads(mavlink_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        mavlink_observation = None
    mavlink_requested = config["mavlinkSwarm"]
    mavlink_passed = (
        isinstance(mavlink_observation, dict) and mavlink_observation.get("status") == "passed"
        if mavlink_requested
        else None
    )
    authorization_requested = config.get("fault") == "cross-vehicle-authorization"
    authorization_path = run_dir / "research-cross-vehicle-authorization.json"
    try:
        authorization_observation = json.loads(authorization_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        authorization_observation = None
    authorization_support = (
        authorization_observation.get("mutationSupport", {}).get("status")
        if isinstance(authorization_observation, dict)
        and isinstance(authorization_observation.get("mutationSupport"), dict)
        else None
    )
    authorization_effects = (
        authorization_observation.get("effectComparison")
        if isinstance(authorization_observation, dict)
        else None
    )
    authorization_effects_valid = (
        isinstance(authorization_effects, dict)
        and authorization_effects.get("runId") == run_dir.name
        and authorization_effects.get("status") == "measured"
        and isinstance(authorization_effects.get("vehicles"), dict)
        and set(authorization_effects["vehicles"]) == {item["callsign"] for item in vehicles}
        and all(
            isinstance(effect, dict)
            and effect.get("model")
            == next(item["model"] for item in vehicles if item["callsign"] == callsign)
            and type(effect.get("poseDisplacementM")) in {int, float}
            and math.isfinite(effect["poseDisplacementM"])
            and effect["poseDisplacementM"] >= 0
            and effect["poseDisplacementM"] <= authorization_effects.get("poseEffectToleranceM", -1)
            and type(effect.get("poseElapsedSimulationSeconds")) in {int, float}
            and effect["poseElapsedSimulationSeconds"] > 0
            and effect.get("actuatorIdentityValid") is True
            and type(effect.get("actuatorSequenceBefore")) is int
            and type(effect.get("actuatorSequenceAfter")) is int
            and effect["actuatorSequenceAfter"] >= effect["actuatorSequenceBefore"]
            for callsign, effect in authorization_effects["vehicles"].items()
        )
    )
    authorization_passed = (
        True
        if authorization_requested
        and isinstance(authorization_observation, dict)
        and authorization_observation.get("runId") == run_dir.name
        and authorization_observation.get("status") == "passed"
        and authorization_observation.get("publishedMessages") == 1
        and authorization_observation.get("controllerDecision", {}).get("status")
        in {"rejected", "rejected-before-active-setpoint"}
        and authorization_effects_valid
        and cross_vehicle_effect_windows_valid(authorization_observation, vehicles, run_dir.name)
        else None
        if authorization_requested and authorization_support == "unsupported"
        else False
        if authorization_requested
        else None
    )
    selected_vehicle_authorization = selected_vehicle_authorization_evidence(
        run_dir, plan, vehicles, run_dir.name
    )
    behavior_passed = behavior_evaluation["status"] == "passed"
    all_identities_match = all(item["runIdentityMatches"] for item in vehicle_results.values())
    all_disarmed_status_available = all(
        item["actuatorEvidence"]["finalDisarmed"]["status"] == "measured"
        for item in vehicle_results.values()
    )
    all_execution_grants_revoked = all(
        item["finalExecutionAuthority"].get("status") == "measured"
        and item["finalExecutionAuthority"].get("executionGrantActive") is False
        for item in vehicle_results.values()
    )
    all_controller_cadences_qualified = all(
        item["controllerStateCadence"].get("status") == "passed"
        for item in vehicle_results.values()
    )
    all_transition_safe_stops_qualified = all(
        item["safeStopEvidence"].get("status") == "measured" for item in vehicle_results.values()
    )
    run_owned_source_build_inputs_present = all(
        provenance_inputs.get(name, {}).get("available") is True
        and isinstance(provenance_inputs.get(name, {}).get("sha256"), str)
        and len(provenance_inputs[name]["sha256"]) == 64
        for name in (
            "gazebo_model_assets",
            "gazebo_source_snapshot_receipt",
            "antoine_build_source_snapshot",
            "antoine_source_snapshot_receipt",
            "antoine_build_receipt",
            "antoine_sitl_binary",
            "antoine_local_authority_binary",
            "antoine_boundary_binary",
        )
    ) and all(
        evidence_artifacts.get(name, {}).get("available") is True
        for name in (
            "research-gazebo-source-snapshot.json",
            "research-antoine-build-receipt.json",
            "research-antoine-process-launches.tsv",
            "gazebo-source-snapshot/gazebo-source-snapshot.json",
            "antoine-source-snapshot/antoine-source-snapshot.json",
            "antoine-binaries/antoine_sitl_nats",
            "antoine-binaries/antoine_gz_local_authority",
            "antoine-binaries/antoine-boundary-service",
        )
    )
    all_vehicles_disarmed = (
        all(
            item["actuatorEvidence"]["finalDisarmed"]["disarmed"] is True
            for item in vehicle_results.values()
        )
        if all_disarmed_status_available
        else None
    )
    minimum_separation = min(
        (
            item["minimumSeparationM"]
            for item in pair_results.values()
            if item["minimumSeparationM"] is not None
        ),
        default=None,
    )
    measured_route_failure = all_pose_streams_passed and any(
        item["runIdentityMatches"]
        and item["independentPosePassed"]
        and any(
            isinstance(leg, dict)
            and (leg.get("kinematicArrivalPassed") is False or leg.get("reached") is False)
            for leg in item["waypoints"]
        )
        for item in vehicle_results.values()
    )
    behavior_status = (
        "failed"
        if measured_route_failure
        or (all_pose_streams_passed and all_pairs_measured and not all_pairs_separated)
        else "inconclusive"
        if formation_time is None
        else "passed"
        if all_flights_passed and all_pairs_separated and behavior_passed
        else "failed"
        if not behavior_passed or (all_pairs_measured and not all_pairs_separated)
        else "inconclusive"
    )
    requested_probe_results = []
    if mavlink_requested:
        requested_probe_results.append(
            "passed"
            if mavlink_passed is True
            else "failed"
            if isinstance(mavlink_observation, dict)
            and mavlink_observation.get("status") == "failed"
            else "inconclusive"
        )
    if authorization_requested:
        requested_probe_results.append(
            "passed"
            if authorization_passed is True
            else "inconclusive"
            if authorization_support == "unsupported"
            or not isinstance(authorization_observation, dict)
            or authorization_observation.get("status") == "inconclusive"
            else "failed"
        )
    if rate_probe_requested:
        requested_probe_results.append(
            rate_probe_observation["status"]
            if rate_probe_observation is not None
            else "inconclusive"
        )
    probe_status = (
        "not_requested"
        if not requested_probe_results
        else "failed"
        if "failed" in requested_probe_results
        else "inconclusive"
        if "inconclusive" in requested_probe_results
        else "passed"
    )
    baseline_gates = {
        "allFlightsPassed": all_flights_passed,
        "allPairwiseSeparationsAtLeast1m": all_pairs_separated,
        "runIdentityMatches": all_identities_match,
        "provenanceStable": provenance_stable is True,
        "poseStreamsOrderedFreshAndCovered": all_pose_streams_passed,
        "allPairwiseSeparationMeasured": all_pairs_measured,
        "actuatorNeutralizationEvidence": all_actuator_evidence_passed,
        "independentPoseAndActuatorArtifactsPresent": independent_artifacts_present,
        "finalDisarmedStateAvailable": all_disarmed_status_available,
        "allVehiclesDisarmed": all_vehicles_disarmed is True,
        "allExecutionGrantsRevoked": all_execution_grants_revoked,
        "controllerStateCadenceQualified": all_controller_cadences_qualified,
        "transitionSafeStopsBoundAndRecovered": all_transition_safe_stops_qualified,
        "runOwnedSourceBuildInputsPresent": run_owned_source_build_inputs_present,
    }
    baseline_qualified = all(baseline_gates.values())
    aggregate_status = (
        "failed"
        if behavior_status == "failed" or probe_status == "failed"
        else "inconclusive"
        if not baseline_qualified
        or behavior_status == "inconclusive"
        or probe_status == "inconclusive"
        else "passed"
    )
    result = {
        "contract": "damn-vulnerable-drone.antoine-swarm-flight",
        "version": SWARM_REPORT_VERSION,
        "runId": run_dir.name,
        "scenario": "independent-antoine-controllers-gazebo-nats",
        "behavior": plan.get("behavior"),
        "swarmPlanSource": plan.get("planSource", "run-artifact"),
        "scope": f"{len(vehicles)} X500 models with independent controllers and local test authorities on one run-owned loopback NATS broker",
        "status": aggregate_status,
        "behaviorStatus": behavior_status,
        "probeStatus": probe_status,
        "baselineStatus": "qualified_baseline" if baseline_qualified else "inconclusive",
        "baselineGates": baseline_gates,
        "baselineInconclusiveReasons": [
            name for name, passed in baseline_gates.items() if not passed
        ],
        "provenance": provenance_summary,
        "evidenceArtifacts": evidence_artifacts,
        "vehicleCount": len(vehicles),
        "vehiclePairCount": pair_count,
        "allVehicleRunIdentityMatches": all_identities_match,
        "threeVehicleRunIdentityMatches": len(vehicles) == 3 and all_identities_match,
        "fiveVehicleRunIdentityMatches": len(vehicles) == 5 and all_identities_match,
        "allFlightsPassed": all_flights_passed,
        "allPairwiseSeparationsMeasured": all_pairs_measured,
        "allPairwiseSeparationsAtLeast1m": all_pairs_separated,
        "allThreePairSeparationsMeasured": pair_count == 3 and all_pairs_measured,
        "allTenPairSeparationsMeasured": pair_count == 10 and all_pairs_measured,
        "behaviorPassed": behavior_passed,
        "controllerStateCadenceQualified": all_controller_cadences_qualified,
        "transitionSafeStopsBoundAndRecovered": all_transition_safe_stops_qualified,
        "runOwnedSourceBuildInputsPresent": run_owned_source_build_inputs_present,
        "behaviorEvaluation": behavior_evaluation,
        "formationEvaluationTimeSeconds": formation_time,
        "mavlinkSwarmRequested": mavlink_requested,
        "mavlinkSwarmPassed": mavlink_passed,
        "mavlinkSwarmObservation": mavlink_observation,
        "crossVehicleAuthorizationRequested": authorization_requested,
        "crossVehicleAuthorizationPassed": authorization_passed,
        "crossVehicleAuthorizationObservation": authorization_observation,
        "selectedVehicleAuthorization": selected_vehicle_authorization,
        "vehicles": vehicle_results,
        "missionProgressArtifact": "research-antoine-progress.json",
        "poseStreamTimeBase": "shared Gazebo world pose header.stamp simulation clock",
        "poseStreamQualityPassed": all_pose_streams_passed,
        "poseStreamQuality": pose_stream_quality,
        "vehiclePairs": pair_results,
        "faultObservation": fault_observation,
        "rateProbeRequested": rate_probe_requested,
        "rateProbeObservation": rate_probe_observation,
        "trajectoryVisualization": "research-swarm-trajectory.svg"
        if trajectory_available
        else None,
        "minimumInterDroneSeparationM": minimum_separation,
        "minimumSeparationThresholdM": MINIMUM_SEPARATION_METERS,
        "posePairToleranceSeconds": POSE_PAIR_TOLERANCE_SECONDS,
        "minimumPoseSamplesPerVehicle": MINIMUM_POSE_SAMPLES_PER_VEHICLE,
        "minimumPoseCoverageSeconds": MINIMUM_POSE_COVERAGE_SECONDS,
        "maximumPoseGapSeconds": MAXIMUM_POSE_GAP_SECONDS,
        "limits": (
            "A local simulation result only. Swarm behaviours are scripted "
            "per-vehicle waypoint plans, not closed-loop formation control, "
            "consensus, or dynamic collision avoidance. The local test authority "
            "is not Core; no hardware safety claim is made. MAVLink evidence, when "
            "requested, covers per-vehicle telemetry fan-out only and does not "
            "grant control of the Gazebo swarm. executionGrantActive measures Core "
            "execution authority only and is not vehicle armed/disarmed state."
        ),
    }
    try:
        validate_swarm_report(result, run_dir.name)
    except ValueError as error:
        raise SystemExit(f"generated swarm report failed contract validation: {error}") from error
    output = run_dir / "research-swarm-flight.json"
    output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    if len(vehicles) == 2:
        (run_dir / "research-two-drone-flight.json").write_text(
            json.dumps(result, indent=2) + "\n", encoding="utf-8"
        )
    print(output)
    print(json.dumps(result, indent=2))
    return 0 if aggregate_status == "passed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
