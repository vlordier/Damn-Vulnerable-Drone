"""Regression coverage for source provenance in Gazebo's mutable asset checkout."""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from capture_antoine_provenance import (
    battery_fix_present,
    changed_input_names,
    changed_source_tree_names,
    source_tree,
    source_tree_changed,
)


class GazeboAssetProvenanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.repo = Path(self.temp_dir.name)
        self.git("init", "-q")
        self.git("config", "user.email", "tests@example.invalid")
        self.git("config", "user.name", "Provenance test")
        (self.repo / "worlds").mkdir()
        (self.repo / "worlds" / "test.sdf").write_text("<sdf version='1.9'/>", encoding="utf-8")
        self.git("add", ".")
        self.git("commit", "-qm", "fixture")

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def git(self, *args: str) -> None:
        subprocess.run(["git", "-C", str(self.repo), *args], check=True, capture_output=True)

    def capture(self) -> dict:
        return source_tree("gazebo_sim_assets", self.repo)

    def test_generated_workspace_outputs_do_not_change_qualification(self) -> None:
        before = self.capture()
        generated = self.repo / "ws" / "install" / "lib" / "generated.py"
        generated.parent.mkdir(parents=True)
        generated.write_text("generated output", encoding="utf-8")
        after = self.capture()

        self.assertNotEqual(before["worktreeStatusSha256"], after["worktreeStatusSha256"])
        self.assertEqual(before["sourceContentSha256"], after["sourceContentSha256"])
        self.assertEqual(before["qualificationStatusSha256"], after["qualificationStatusSha256"])
        self.assertFalse(source_tree_changed(before, after))

    def test_battery_patch_is_recognized_after_source_evolution(self) -> None:
        source = self.repo / "antoine_sitl_nats.rs"
        source.write_text(
            "\n".join(
                (
                    "fn battery_after_drain(remaining_pct: f64, drain_pct_per_s: f64, elapsed_s: f64) {",
                    "battery_pct = battery_after_drain(battery_pct, battery_drain_pct_per_s, dt)",
                    "fn gazebo_plant_honors_configured_battery_drain_rate() {",
                )
            ),
            encoding="utf-8",
        )
        self.assertTrue(battery_fix_present(source))
        source.write_text("fn battery_after_drain() {}", encoding="utf-8")
        self.assertFalse(battery_fix_present(source))

    def test_tracked_source_content_change_is_detected_even_if_status_is_same(self) -> None:
        source = self.repo / "worlds" / "test.sdf"
        source.write_text("<sdf version='1.9'><world name='first'/></sdf>", encoding="utf-8")
        before = self.capture()
        source.write_text("<sdf version='1.9'><world name='second'/></sdf>", encoding="utf-8")
        after = self.capture()

        self.assertEqual(before["changedPathCount"], after["changedPathCount"])
        self.assertNotEqual(before["sourceContentSha256"], after["sourceContentSha256"])
        self.assertTrue(source_tree_changed(before, after))

    def test_untracked_source_file_change_is_detected(self) -> None:
        source = self.repo / "worlds" / "runtime-overlay.sdf"
        source.write_text("<sdf version='1.9'><world name='first'/></sdf>", encoding="utf-8")
        before = self.capture()
        source.write_text("<sdf version='1.9'><world name='second'/></sdf>", encoding="utf-8")
        after = self.capture()

        self.assertNotEqual(before["sourceContentSha256"], after["sourceContentSha256"])
        self.assertTrue(source_tree_changed(before, after))

    def test_c2_checkout_context_does_not_hide_exact_bridge_input_changes(self) -> None:
        before_trees = {
            "c2_bridge_source": {"available": True, "revision": "same", "worktreeStatusSha256": "a"}
        }
        after_trees = {
            "c2_bridge_source": {"available": True, "revision": "same", "worktreeStatusSha256": "b"}
        }
        self.assertEqual(changed_source_tree_names(before_trees, after_trees), [])

        before_inputs = {"c2_gazebo_bridge": {"available": True, "sha256": "script-a"}}
        after_inputs = {"c2_gazebo_bridge": {"available": True, "sha256": "script-b"}}
        self.assertEqual(changed_input_names(before_inputs, after_inputs), ["c2_gazebo_bridge"])

    def test_gazebo_checkout_is_context_when_run_owned_assets_are_bound(self) -> None:
        before_trees = {
            "gazebo_sim_assets": {
                "available": True,
                "revision": "same",
                "qualificationStatusSha256": "dirty-before",
                "sourceContentSha256": "source-before",
            }
        }
        after_trees = {
            "gazebo_sim_assets": {
                "available": True,
                "revision": "same",
                "qualificationStatusSha256": "dirty-after",
                "sourceContentSha256": "source-after",
            }
        }
        self.assertTrue(
            source_tree_changed(before_trees["gazebo_sim_assets"], after_trees["gazebo_sim_assets"])
        )
        self.assertEqual(changed_source_tree_names(before_trees, after_trees), [])
        before_inputs = {
            "gazebo_model_assets": {"available": True, "sha256": "run-snapshot"},
            "gazebo_source_snapshot_receipt": {"available": True, "sha256": "receipt"},
        }
        after_inputs = dict(before_inputs)
        self.assertEqual(changed_input_names(before_inputs, after_inputs), [])
        after_inputs["gazebo_model_assets"] = {"available": True, "sha256": "mutated-snapshot"}
        self.assertEqual(changed_input_names(before_inputs, after_inputs), ["gazebo_model_assets"])


if __name__ == "__main__":
    unittest.main()
