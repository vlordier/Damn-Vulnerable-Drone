#!/usr/bin/env python3
"""Plan and evaluate bounded, scripted swarm geometry for the Antoine lab.

The Antoine Gazebo profile gives each vehicle an independent local authority.
That authority follows a fixed waypoint list and does not accept live formation
setpoints from another process.  The behaviours here are therefore *scripted
plans*: every vehicle receives its own waypoint list up front, and the result is
measured afterwards from independent Gazebo poses.  They are not closed-loop
formation control, consensus, or dynamic collision avoidance.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any

BEHAVIORS = ("lanes", "column", "vee", "rendezvous", "split")
CONTRACT = "damn-vulnerable-drone.antoine-swarm-plan"
DEFAULT_ALTITUDE_M = 10.0
DEFAULT_TRAVEL_M = 8.0
MIN_VEHICLES = 2
MAX_VEHICLES = 5
MIN_SPACING_M = 2.0
MAX_SPACING_M = 20.0
SPAWN_ALTITUDE_M = 8.0
RENDEZVOUS_MIN_SPACING_M = 4.0
# Non-lane behaviours converge across lanes, so each vehicle transits at its
# own altitude layer and only descends once it is at its separated final slot.
ALTITUDE_LAYER_M = 2.0
MIN_PLANNED_CLEARANCE_M = 1.0
# Conservative budget for the runner's --flight-seconds default. The local
# authority's default horizontal cap is 0.6 m/s and its vertical target is
# lower, so 0.5 m/s effective travel plus fixed settle/dwell overhead keeps a
# 5-vehicle scripted transit inside the 180 s launcher limit.
TRANSIT_SPEED_MPS = 0.5
FLIGHT_OVERHEAD_S = 30.0
PER_WAYPOINT_OVERHEAD_S = 8.0


def suggested_flight_seconds(plan: dict[str, Any]) -> int:
    """Return a conservative per-vehicle flight budget for ``plan``.

    This is a launch-time budget, not a measured duration. It assumes a slower
    effective transit than the local authority's 0.6 m/s horizontal cap because
    vertical legs and per-waypoint dwell are slower.
    """
    vehicles = plan.get("vehicles")
    if not isinstance(vehicles, list) or not vehicles:
        raise ValueError("plan must declare at least one vehicle")
    longest_path = max(_path_length(vehicle["waypointsEnuM"]) for vehicle in vehicles)
    waypoint_count = max(len(vehicle["waypointsEnuM"]) for vehicle in vehicles)
    seconds = math.ceil(
        longest_path / TRANSIT_SPEED_MPS
        + PER_WAYPOINT_OVERHEAD_S * (waypoint_count - 1)
        + FLIGHT_OVERHEAD_S
    )
    return max(20, min(180, seconds))


def _finite(value: float) -> bool:
    return isinstance(value, (int, float)) and math.isfinite(float(value))


def _round_point(point: tuple[float, float, float]) -> list[float]:
    return [round(float(value), 3) for value in point]


def _spawn_point(index: int, spacing_m: float) -> tuple[float, float, float]:
    # prepare_antoine_two_drone_world.py places the roster line-abreast on +X.
    return (index * spacing_m, 0.0, SPAWN_ALTITUDE_M)


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def _segment_distance(
    first_start: list[float],
    first_end: list[float],
    second_start: list[float],
    second_end: list[float],
) -> float:
    """Shortest distance between two 3D segments (Ericson, clamped)."""
    first_direction = [first_end[i] - first_start[i] for i in range(3)]
    second_direction = [second_end[i] - second_start[i] for i in range(3)]
    offset = [first_start[i] - second_start[i] for i in range(3)]
    first_length = sum(value * value for value in first_direction)
    second_length = sum(value * value for value in second_direction)

    if first_length <= 1e-12 and second_length <= 1e-12:
        return math.dist(first_start, second_start)
    if first_length <= 1e-12:
        first_parameter = 0.0
        second_parameter = _clamp(
            sum(second_direction[i] * offset[i] for i in range(3)) / second_length
        )
    else:
        first_offset = sum(first_direction[i] * offset[i] for i in range(3))
        if second_length <= 1e-12:
            second_parameter = 0.0
            first_parameter = _clamp(-first_offset / first_length)
        else:
            cross = sum(first_direction[i] * second_direction[i] for i in range(3))
            denominator = first_length * second_length - cross * cross
            if denominator > 1e-12:
                second_offset = sum(second_direction[i] * offset[i] for i in range(3))
                first_parameter = _clamp(
                    (cross * second_offset - first_offset * second_length) / denominator
                )
            else:
                first_parameter = 0.0
            second_parameter = (
                cross * first_parameter + sum(second_direction[i] * offset[i] for i in range(3))
            ) / second_length
            if second_parameter < 0.0:
                second_parameter = 0.0
                first_parameter = _clamp(-first_offset / first_length)
            elif second_parameter > 1.0:
                second_parameter = 1.0
                first_parameter = _clamp((cross - first_offset) / first_length)

    first_point = [first_start[i] + first_parameter * first_direction[i] for i in range(3)]
    second_point = [second_start[i] + second_parameter * second_direction[i] for i in range(3)]
    return math.dist(first_point, second_point)


def _path_length(path: list[list[float]]) -> float:
    return sum(math.dist(start, end) for start, end in zip(path, path[1:], strict=False))


def _point_at_distance(path: list[list[float]], distance: float) -> list[float]:
    remaining = distance
    for start, end in zip(path, path[1:], strict=False):
        segment = math.dist(start, end)
        if segment <= 1e-12:
            continue
        if remaining <= segment:
            fraction = remaining / segment
            return [start[i] + fraction * (end[i] - start[i]) for i in range(3)]
        remaining -= segment
    return path[-1]


def _static_path_clearance(waypoints_by_vehicle: list[list[list[float]]]) -> float:
    minimum = math.inf
    for first_index, first_path in enumerate(waypoints_by_vehicle):
        for second_path in waypoints_by_vehicle[first_index + 1 :]:
            for first_start, first_end in zip(first_path, first_path[1:], strict=False):
                for second_start, second_end in zip(second_path, second_path[1:], strict=False):
                    minimum = min(
                        minimum,
                        _segment_distance(first_start, first_end, second_start, second_end),
                    )
    return minimum


def _synchronized_path_clearance(
    waypoints_by_vehicle: list[list[list[float]]], sample_step_m: float = 0.1
) -> float:
    """Estimate separation if every vehicle starts together at the same speed.

    This is a deterministic plan-time approximation, not a flight measurement.
    It catches simultaneous conflicts that a plain geometric path-intersection
    check would over-report for an in-trail column.
    """
    lengths = [_path_length(path) for path in waypoints_by_vehicle]
    maximum = max(lengths, default=0.0)
    if maximum <= 0.0:
        return math.inf
    samples = max(1, int(math.ceil(maximum / sample_step_m)))
    minimum = math.inf
    for step in range(samples + 1):
        distance = maximum * step / samples
        points = [
            _point_at_distance(path, min(distance, length))
            for path, length in zip(waypoints_by_vehicle, lengths, strict=False)
        ]
        for first_index, first in enumerate(points):
            for second in points[first_index + 1 :]:
                minimum = min(minimum, math.dist(first, second))
    return minimum


def _behavior_slots(
    behavior: str, vehicle_count: int, spacing_m: float
) -> tuple[list[tuple[float, float]], list[str], list[str | None]]:
    """Return two-dimensional slots, roles, and optional split groups."""
    roles = ["leader"] + ["wing"] * (vehicle_count - 1)
    groups: list[str | None] = [None] * vehicle_count

    if behavior == "lanes":
        slots = [(index * spacing_m, 0.0) for index in range(vehicle_count)]
    elif behavior == "column":
        # In-trail formation behind the leader, ordered by callsign.  Keep the
        # trail short enough that a five-vehicle run stays inside its time bound.
        trail = max(5.0, min(spacing_m, 6.0))
        slots = [(0.0, -index * trail) for index in range(vehicle_count)]
    elif behavior == "vee":
        slots = [(0.0, 0.0)]
        for index in range(1, vehicle_count):
            rank = (index + 1) // 2
            side = 1.0 if index % 2 else -1.0
            slots.append((-rank * spacing_m * 0.75, side * rank * spacing_m * 0.65))
    elif behavior == "rendezvous":
        if spacing_m < RENDEZVOUS_MIN_SPACING_M:
            raise ValueError(f"rendezvous needs at least {RENDEZVOUS_MIN_SPACING_M:g} m spacing")
        centre_x = (vehicle_count - 1) * spacing_m / 2.0
        # Keep neighbouring final slots at least 5.5 m apart on the ring. This
        # is a static target geometry, not a dynamic separation guarantee.
        target_chord = max(5.5, spacing_m * 0.5)
        radius = max(2.75, target_chord / (2.0 * math.sin(math.pi / vehicle_count)))
        slots = [
            (
                centre_x
                + radius * math.cos(-math.pi / 2.0 + 2.0 * math.pi * index / vehicle_count),
                radius * math.sin(-math.pi / 2.0 + 2.0 * math.pi * index / vehicle_count),
            )
            for index in range(vehicle_count)
        ]
    elif behavior == "split":
        centre_x = max(spacing_m * 1.25, 6.0)
        group_index = {"a": 0, "b": 0}
        slots = []
        for index in range(vehicle_count):
            group = "a" if index % 2 == 0 else "b"
            groups[index] = group
            slot_x = centre_x if group == "a" else -centre_x
            slots.append((slot_x, group_index[group] * max(5.5, spacing_m * 0.75)))
            group_index[group] += 1
        roles = ["leader" if group == "a" else "follower" for group in groups]
    else:  # pragma: no cover - guarded by callers
        raise ValueError(f"unsupported swarm behaviour: {behavior}")

    return slots, roles, groups


def build_plan(
    behavior: str,
    vehicle_count: int,
    spacing_m: float,
    travel_m: float = DEFAULT_TRAVEL_M,
    altitude_m: float = DEFAULT_ALTITUDE_M,
    models: list[str] | None = None,
) -> dict[str, Any]:
    """Build a deterministic per-vehicle waypoint plan."""
    if behavior not in BEHAVIORS:
        raise ValueError(f"behaviour must be one of: {', '.join(BEHAVIORS)}")
    if not isinstance(vehicle_count, int) or not MIN_VEHICLES <= vehicle_count <= MAX_VEHICLES:
        raise ValueError(f"vehicle count must be {MIN_VEHICLES}..{MAX_VEHICLES}")
    if not _finite(spacing_m) or not MIN_SPACING_M <= float(spacing_m) <= MAX_SPACING_M:
        raise ValueError(f"spacing must be {MIN_SPACING_M:g}..{MAX_SPACING_M:g} metres")
    if not _finite(travel_m) or not 4.0 <= float(travel_m) <= 20.0:
        raise ValueError("travel must be 4..20 metres")
    if not _finite(altitude_m) or not 5.0 <= float(altitude_m) <= 30.0:
        raise ValueError("altitude must be 5..30 metres")
    if models is not None and (len(models) != vehicle_count or len(set(models)) != vehicle_count):
        raise ValueError("models must be unique and match the vehicle count")

    spacing = float(spacing_m)
    slots, roles, groups = _behavior_slots(behavior, vehicle_count, spacing)
    travel = float(travel_m)
    altitude = float(altitude_m)

    first_slot: tuple[float, float, float] | None = None
    travel_vector = (0.0, travel, 0.0)
    vehicles = []
    for index, (slot_x, slot_y) in enumerate(slots):
        slot = (slot_x, slot_y, altitude)
        translated = (slot[0] + travel_vector[0], slot[1] + travel_vector[1], slot[2])
        spawn = _spawn_point(index, spacing)
        if behavior == "lanes":
            waypoints = [slot, translated]
            cruise_altitude = altitude
        else:
            # Cross-lane behaviours transit at a per-vehicle altitude layer and
            # descend only at their separated final slot.
            cruise_altitude = altitude + index * ALTITUDE_LAYER_M
            waypoints = [
                (spawn[0], spawn[1], cruise_altitude),
                (slot_x, slot_y, cruise_altitude),
                (translated[0], translated[1], altitude),
            ]
        if first_slot is None:
            first_slot = waypoints[0]
        entry: dict[str, Any] = {
            "callsign": f"drone-{index + 1}",
            "role": roles[index],
            "spawnEnuM": _round_point(spawn),
            "cruiseAltitudeM": round(cruise_altitude, 3),
            "waypointsEnuM": [_round_point(point) for point in waypoints],
            "finalSlotEnuM": _round_point(translated),
        }
        if models is not None:
            entry["model"] = models[index]
        if groups[index] is not None:
            entry["group"] = groups[index]
        vehicles.append(entry)

    paths = [vehicle["waypointsEnuM"] for vehicle in vehicles]
    static_clearance = _static_path_clearance(paths)
    synchronized_clearance = _synchronized_path_clearance(paths)
    if synchronized_clearance < MIN_PLANNED_CLEARANCE_M:
        raise ValueError(
            f"planned synchronized paths pass within {synchronized_clearance:.2f} m, "
            f"below the {MIN_PLANNED_CLEARANCE_M:g} m lab floor"
        )

    return {
        "contract": CONTRACT,
        "version": "1.1.0",
        "behavior": behavior,
        "description": _behavior_description(behavior),
        "spacingM": round(spacing, 3),
        "travelM": round(travel, 3),
        "altitudeM": round(altitude, 3),
        "altitudeLayerM": round(ALTITUDE_LAYER_M, 3) if behavior != "lanes" else 0.0,
        "plannedSynchronizedClearanceM": round(synchronized_clearance, 3),
        "plannedStaticPathClearanceM": round(static_clearance, 3),
        "vehicleCount": vehicle_count,
        "firstWaypointEnuM": _round_point(first_slot),
        "vehicles": vehicles,
        "limits": (
            "Scripted waypoint geometry only. Non-lane behaviours use fixed "
            "per-vehicle altitude layers during transit and descend at separated "
            "final slots; this is static scheduling, not dynamic collision "
            "avoidance. Every vehicle is commanded by an independent local "
            "authority; there is no inter-vehicle feedback, formation controller, "
            "or consensus loop. Pose checks qualify the planned geometry, not "
            "swarming autonomy."
        ),
    }


def _behavior_description(behavior: str) -> str:
    return {
        "lanes": "Parallel lanes with fixed lateral spacing.",
        "column": "Single-file in-trail geometry behind the leader.",
        "vee": "Leader with alternating left/right wing positions behind it.",
        "rendezvous": "Converge from spawn line-abreast onto a ring, then translate.",
        "split": "Depart the spawn line into two separated subgroups.",
    }[behavior]


def waypoint_string(points: list[list[float]]) -> str:
    return ";".join(",".join(f"{value:g}" for value in point) for point in points)


def shell_paths(plan: dict[str, Any]) -> tuple[str, str]:
    """Return (leader path, secondary paths joined by '|') for the launcher."""
    vehicles = plan["vehicles"]
    leader = waypoint_string(vehicles[0]["waypointsEnuM"])
    secondary = "|".join(waypoint_string(vehicle["waypointsEnuM"]) for vehicle in vehicles[1:])
    return leader, secondary


def _distance(first: tuple[float, float, float], second: tuple[float, float, float]) -> float:
    return math.dist(first, second)


def _samples_for(
    plan: dict[str, Any], poses_by_callsign: dict[str, list[tuple[float, float, float, float]]]
) -> dict[str, list[tuple[float, float, float, float]]]:
    samples = {}
    for vehicle in plan["vehicles"]:
        raw = poses_by_callsign.get(vehicle["callsign"], [])
        cleaned = [
            (float(stamp), float(x), float(y), float(z))
            for stamp, x, y, z in raw
            if all(_finite(value) for value in (stamp, x, y, z))
        ]
        samples[vehicle["callsign"]] = sorted(cleaned, key=lambda sample: sample[0])
    return samples


def evaluate_plan(
    plan: dict[str, Any],
    poses_by_callsign: dict[str, list[tuple[float, float, float, float]]],
    min_separation_m: float = 1.0,
    final_tolerance_m: float = 3.0,
) -> dict[str, Any]:
    """Check planned final geometry and behaviour-specific pose gates."""
    if plan.get("contract") != CONTRACT:
        raise ValueError("unsupported swarm plan contract")
    vehicles = plan.get("vehicles")
    if not isinstance(vehicles, list) or not MIN_VEHICLES <= len(vehicles) <= MAX_VEHICLES:
        raise ValueError("swarm plan must declare 2..5 vehicles")
    if not _finite(min_separation_m) or float(min_separation_m) < 0.0:
        raise ValueError("minimum separation must be a non-negative number")
    if not _finite(final_tolerance_m) or not 0.1 <= float(final_tolerance_m) <= 10.0:
        raise ValueError("final slot tolerance must be 0.1..10 metres")

    samples = _samples_for(plan, poses_by_callsign)
    vehicle_results: dict[str, Any] = {}
    finals: dict[str, tuple[float, float, float]] = {}
    measured = True
    for vehicle in vehicles:
        callsign = vehicle["callsign"]
        model_samples = samples.get(callsign, [])
        planned = tuple(float(value) for value in vehicle["finalSlotEnuM"])
        if len(model_samples) < 2:
            measured = False
            vehicle_results[callsign] = {
                "role": vehicle.get("role"),
                "group": vehicle.get("group"),
                "plannedFinalSlotEnuM": list(planned),
                "poseSampleCount": len(model_samples),
                "measurementStatus": "unavailable",
            }
            continue
        first = model_samples[0][1:]
        last = model_samples[-1][1:]
        error = _distance(last, planned)
        finals[callsign] = last
        vehicle_results[callsign] = {
            "role": vehicle.get("role"),
            "group": vehicle.get("group"),
            "plannedFinalSlotEnuM": list(planned),
            "initialPositionEnuM": _round_point(first),
            "finalPositionEnuM": _round_point(last),
            "finalSlotErrorM": round(error, 3),
            "finalSlotWithinTolerance": error <= float(final_tolerance_m),
            "poseSampleCount": len(model_samples),
            "measurementStatus": "measured",
        }

    callsigns = [vehicle["callsign"] for vehicle in vehicles]
    planned_final = {
        vehicle["callsign"]: tuple(float(value) for value in vehicle["finalSlotEnuM"])
        for vehicle in vehicles
    }
    initial_positions = {
        callsign: tuple(result["initialPositionEnuM"])
        for callsign, result in vehicle_results.items()
        if "initialPositionEnuM" in result
    }

    pair_results = {}
    for first_index, first_callsign in enumerate(callsigns):
        for second_callsign in callsigns[first_index + 1 :]:
            if first_callsign not in finals or second_callsign not in finals:
                continue
            planned_distance = _distance(
                planned_final[first_callsign], planned_final[second_callsign]
            )
            observed_distance = _distance(finals[first_callsign], finals[second_callsign])
            pair_results[f"{first_callsign}/{second_callsign}"] = {
                "plannedFinalDistanceM": round(planned_distance, 3),
                "observedFinalDistanceM": round(observed_distance, 3),
                "finalDistanceErrorM": round(abs(observed_distance - planned_distance), 3),
            }

    all_measured = measured and len(finals) == len(callsigns)
    all_slots = all_measured and all(
        result.get("finalSlotWithinTolerance") is True for result in vehicle_results.values()
    )
    shape_tolerance = 2.0 * float(final_tolerance_m)
    shape_matched = (
        all_measured
        and bool(pair_results)
        and all(pair["finalDistanceErrorM"] <= shape_tolerance for pair in pair_results.values())
    )
    final_separation = (
        min(
            (pair["observedFinalDistanceM"] for pair in pair_results.values()),
            default=None,
        )
        if pair_results
        else None
    )
    final_separated = final_separation is not None and final_separation >= float(min_separation_m)

    initial_span = (
        max(
            _distance(initial_positions[first], initial_positions[second])
            for first_index, first in enumerate(callsigns)
            for second in callsigns[first_index + 1 :]
            if first in initial_positions and second in initial_positions
        )
        if len(initial_positions) == len(callsigns)
        else None
    )
    final_span = (
        max(pair["observedFinalDistanceM"] for pair in pair_results.values())
        if pair_results
        else None
    )
    planned_final_span = (
        max(
            _distance(planned_final[first], planned_final[second])
            for first_index, first in enumerate(callsigns)
            for second in callsigns[first_index + 1 :]
        )
        if len(callsigns) > 1
        else None
    )

    behavior = plan.get("behavior")
    rendezvous_converged = None
    split_separated = None
    if behavior == "rendezvous":
        minimum_reduction = max(2.0, 0.2 * float(plan.get("spacingM", 2.0)))
        rendezvous_converged = bool(
            initial_span is not None
            and final_span is not None
            and planned_final_span is not None
            and (initial_span - final_span) >= minimum_reduction
            and final_span <= planned_final_span + shape_tolerance
        )
    elif behavior == "split":
        minimum_between = max(float(plan.get("spacingM", 2.0)), 4.0)
        groups = {
            vehicle["callsign"]: vehicle.get("group")
            for vehicle in vehicles
            if vehicle.get("group") in {"a", "b"}
        }
        between = []
        for first_index, first in enumerate(callsigns):
            for second in callsigns[first_index + 1 :]:
                if groups.get(first) != groups.get(second) and first in finals and second in finals:
                    between.append(_distance(finals[first], finals[second]))
        split_separated = bool(between) and min(between) >= minimum_between

    gates: dict[str, bool] = {
        "allFinalPositionsMeasured": all_measured,
        "allFinalSlotsWithinTolerance": all_slots,
        "plannedFormationShapeMatched": shape_matched,
        "finalPairSeparationAtLeastThreshold": final_separated,
    }
    required_gates = [
        "allFinalPositionsMeasured",
        "allFinalSlotsWithinTolerance",
        "plannedFormationShapeMatched",
        "finalPairSeparationAtLeastThreshold",
    ]
    if rendezvous_converged is not None:
        gates["rendezvousConverged"] = rendezvous_converged
        required_gates.append("rendezvousConverged")
    if split_separated is not None:
        gates["splitGroupsSeparated"] = split_separated
        required_gates.append("splitGroupsSeparated")

    return {
        "contract": "damn-vulnerable-drone.antoine-swarm-behavior-evaluation",
        "version": "1.0.0",
        "behavior": behavior,
        "status": "passed" if all(gates[name] for name in required_gates) else "inconclusive",
        "finalSlotToleranceM": round(float(final_tolerance_m), 3),
        "formationShapeToleranceM": round(shape_tolerance, 3),
        "minimumSeparationThresholdM": round(float(min_separation_m), 3),
        "vehicles": vehicle_results,
        "pairs": pair_results,
        "initialGroupSpanM": round(initial_span, 3) if initial_span is not None else None,
        "finalGroupSpanM": round(final_span, 3) if final_span is not None else None,
        "plannedFinalGroupSpanM": round(planned_final_span, 3)
        if planned_final_span is not None
        else None,
        "minimumFinalPairSeparationM": round(final_separation, 3)
        if final_separation is not None
        else None,
        "gates": gates,
        "requiredGates": required_gates,
        "limits": (
            "Measured from Gazebo pose truth after a scripted flight. These gates "
            "qualify the planned geometry and pairwise separation only; they do not "
            "prove closed-loop formation control, consensus, or collision avoidance."
        ),
    }


def _write_json(value: dict[str, Any], output: Path | None) -> None:
    payload = json.dumps(value, indent=2) + "\n"
    if output is None:
        sys.stdout.write(payload)
    else:
        output.write_text(payload, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name in ("plan", "shell", "flight-seconds"):
        command = subparsers.add_parser(name)
        command.add_argument("--behavior", choices=BEHAVIORS, required=True)
        command.add_argument("--size", type=int, required=True)
        command.add_argument("--spacing", type=float, required=True)
        command.add_argument("--travel", type=float, default=DEFAULT_TRAVEL_M)
        command.add_argument("--altitude", type=float, default=DEFAULT_ALTITUDE_M)
        command.add_argument("--output", type=Path)
        if name == "plan":
            command.add_argument("--models", help="comma-separated model roster")
    args = parser.parse_args()

    models = None
    if getattr(args, "models", None):
        models = args.models.split(",")
    try:
        plan = build_plan(
            args.behavior,
            args.size,
            args.spacing,
            travel_m=args.travel,
            altitude_m=args.altitude,
            models=models,
        )
    except ValueError as error:
        print(f"swarm plan rejected: {error}", file=sys.stderr)
        return 2

    if args.command == "shell":
        leader, secondary = shell_paths(plan)
        print(leader)
        print(secondary)
        return 0
    if args.command == "flight-seconds":
        print(suggested_flight_seconds(plan))
        return 0
    _write_json(plan, args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
