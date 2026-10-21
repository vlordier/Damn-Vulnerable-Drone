"""Unit tests for scripted swarm planning and pose-based evaluation."""

from __future__ import annotations

import unittest

from swarm_behavior import (
    ALTITUDE_LAYER_M,
    BEHAVIORS,
    MIN_PLANNED_CLEARANCE_M,
    build_plan,
    evaluate_plan,
    shell_paths,
    suggested_flight_seconds,
    waypoint_string,
)


def perfect_poses(plan):
    """Build two-sample pose histories that end exactly on every planned slot."""
    poses = {}
    for index, vehicle in enumerate(plan["vehicles"]):
        spawn = tuple(vehicle["spawnEnuM"])
        final = tuple(vehicle["finalSlotEnuM"])
        poses[vehicle["callsign"]] = [
            (float(index) * 0.01, *spawn),
            (1.0 + float(index) * 0.01, *final),
        ]
    return poses


class SwarmPlanTests(unittest.TestCase):
    def test_every_behavior_has_distinct_slots_and_safe_spacing(self) -> None:
        for behavior in BEHAVIORS:
            for size in range(2, 6):
                with self.subTest(behavior=behavior, size=size):
                    plan = build_plan(behavior, size, 8.0)
                    finals = [tuple(vehicle["finalSlotEnuM"]) for vehicle in plan["vehicles"]]
                    self.assertEqual(len(finals), size)
                    self.assertEqual(len(set(finals)), size)
                    for index, first in enumerate(finals):
                        for second in finals[index + 1 :]:
                            distance = (
                                sum((a - b) ** 2 for a, b in zip(first, second, strict=False))
                                ** 0.5
                            )
                            self.assertGreaterEqual(distance, 2.0)

    def test_non_lane_paths_use_distinct_altitude_layers_then_descend(self) -> None:
        for behavior in BEHAVIORS:
            if behavior == "lanes":
                continue
            with self.subTest(behavior=behavior):
                plan = build_plan(behavior, 4, 8.0)
                self.assertEqual(plan["altitudeLayerM"], ALTITUDE_LAYER_M)
                for index, vehicle in enumerate(plan["vehicles"]):
                    self.assertEqual(len(vehicle["waypointsEnuM"]), 3)
                    self.assertAlmostEqual(
                        vehicle["cruiseAltitudeM"],
                        plan["altitudeM"] + index * ALTITUDE_LAYER_M,
                    )
                    self.assertAlmostEqual(vehicle["waypointsEnuM"][-1][2], plan["altitudeM"])

    def test_default_spacing_keeps_final_slots_at_or_above_the_s1_reference(self) -> None:
        # S1's SafetyConfig default minimum separation is 5.0 m. This lab does
        # not run S1 ORCA, but its default final slots should not sit closer.
        for behavior in BEHAVIORS:
            for size in range(2, 6):
                with self.subTest(behavior=behavior, size=size):
                    plan = build_plan(behavior, size, 8.0)
                    finals = [tuple(vehicle["finalSlotEnuM"]) for vehicle in plan["vehicles"]]
                    for index, first in enumerate(finals):
                        for second in finals[index + 1 :]:
                            self.assertGreaterEqual(
                                sum((a - b) ** 2 for a, b in zip(first, second, strict=False))
                                ** 0.5,
                                5.0,
                            )

    def test_plan_records_clearance_and_meets_the_scripted_floor(self) -> None:
        for behavior in BEHAVIORS:
            for size in range(2, 6):
                with self.subTest(behavior=behavior, size=size):
                    plan = build_plan(behavior, size, 8.0)
                    self.assertGreaterEqual(
                        plan["plannedSynchronizedClearanceM"],
                        MIN_PLANNED_CLEARANCE_M,
                    )
                    self.assertGreaterEqual(plan["plannedSynchronizedClearanceM"], 2.0)
                    self.assertGreaterEqual(plan["plannedStaticPathClearanceM"], 0.0)

    def test_suggested_flight_seconds_scales_with_transit_length(self) -> None:
        lanes = build_plan("lanes", 2, 8.0)
        vee_two = build_plan("vee", 2, 8.0)
        vee_five = build_plan("vee", 5, 8.0)
        self.assertEqual(suggested_flight_seconds(lanes), 54)
        self.assertGreater(suggested_flight_seconds(vee_two), suggested_flight_seconds(lanes))
        self.assertGreater(suggested_flight_seconds(vee_five), suggested_flight_seconds(vee_two))
        self.assertLessEqual(suggested_flight_seconds(vee_five), 180)

    def test_lanes_preserve_the_existing_parallel_lane_contract(self) -> None:
        plan = build_plan("lanes", 2, 8.0)
        leader, secondary = shell_paths(plan)
        self.assertEqual(leader, "0,0,10;0,8,10")
        self.assertEqual(secondary, "8,0,10;8,8,10")

    def test_rendezvous_rejects_spacing_too_tight_to_converge(self) -> None:
        with self.assertRaises(ValueError):
            build_plan("rendezvous", 3, 3.0)

    def test_invalid_roster_and_spacing_are_rejected(self) -> None:
        for size, spacing in ((1, 8.0), (6, 8.0), (2, 1.0), (2, 21.0)):
            with self.subTest(size=size, spacing=spacing):
                with self.assertRaises(ValueError):
                    build_plan("lanes", size, spacing)

    def test_waypoint_string_is_launcher_compatible(self) -> None:
        self.assertEqual(
            waypoint_string([[0.0, 0.0, 10.0], [1.5, 8.0, 10.0]]),
            "0,0,10;1.5,8,10",
        )


class SwarmEvaluationTests(unittest.TestCase):
    def test_perfect_scripted_geometries_pass(self) -> None:
        for behavior in BEHAVIORS:
            with self.subTest(behavior=behavior):
                plan = build_plan(behavior, 3, 8.0)
                evaluation = evaluate_plan(plan, perfect_poses(plan))
                self.assertEqual(evaluation["status"], "passed", evaluation["gates"])
                self.assertTrue(all(evaluation["gates"].values()))

    def test_final_slot_deviation_fails_closed(self) -> None:
        plan = build_plan("vee", 3, 8.0)
        poses = perfect_poses(plan)
        callsign = plan["vehicles"][1]["callsign"]
        stamp, x, y, z = poses[callsign][-1]
        poses[callsign][-1] = (stamp, x + 10.0, y, z)
        evaluation = evaluate_plan(plan, poses)
        self.assertEqual(evaluation["status"], "inconclusive")
        self.assertFalse(evaluation["gates"]["allFinalSlotsWithinTolerance"])

    def test_missing_pose_history_fails_closed(self) -> None:
        plan = build_plan("lanes", 2, 8.0)
        poses = perfect_poses(plan)
        poses[plan["vehicles"][1]["callsign"]] = []
        evaluation = evaluate_plan(plan, poses)
        self.assertEqual(evaluation["status"], "inconclusive")
        self.assertFalse(evaluation["gates"]["allFinalPositionsMeasured"])

    def test_rendezvous_must_actually_converge(self) -> None:
        plan = build_plan("rendezvous", 3, 8.0)
        poses = {}
        for index, vehicle in enumerate(plan["vehicles"]):
            final = tuple(vehicle["finalSlotEnuM"])
            poses[vehicle["callsign"]] = [
                (float(index) * 0.01, *final),
                (1.0 + float(index) * 0.01, *final),
            ]
        evaluation = evaluate_plan(plan, poses)
        self.assertEqual(evaluation["status"], "inconclusive")
        self.assertFalse(evaluation["gates"]["rendezvousConverged"])

    def test_split_must_separate_the_subgroups(self) -> None:
        plan = build_plan("split", 4, 8.0)
        poses = perfect_poses(plan)
        for vehicle in plan["vehicles"]:
            poses[vehicle["callsign"]][-1] = (2.0, 0.0, 0.0, 10.0)
        evaluation = evaluate_plan(plan, poses)
        self.assertEqual(evaluation["status"], "inconclusive")
        self.assertFalse(evaluation["gates"]["splitGroupsSeparated"])

    def test_collision_below_threshold_fails(self) -> None:
        plan = build_plan("lanes", 2, 8.0)
        poses = perfect_poses(plan)
        first, second = plan["vehicles"]
        shared = (2.0, 0.0, 0.0, 10.0)
        poses[first["callsign"]][-1] = shared
        poses[second["callsign"]][-1] = (2.1, 0.1, 0.0, 10.0)
        evaluation = evaluate_plan(plan, poses, min_separation_m=1.0)
        self.assertEqual(evaluation["status"], "inconclusive")
        self.assertFalse(evaluation["gates"]["finalPairSeparationAtLeastThreshold"])

    def test_bad_plan_contract_is_rejected(self) -> None:
        plan = build_plan("lanes", 2, 8.0)
        plan["contract"] = "not-a-swarm-plan"
        with self.assertRaises(ValueError):
            evaluate_plan(plan, perfect_poses(plan))


if __name__ == "__main__":
    unittest.main()
