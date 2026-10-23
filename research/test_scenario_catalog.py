from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from dvd_research.scenario_catalog import CATALOG_CONTRACT, load_scenario_catalog


class ScenarioCatalogTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.catalog_path = self.root / "scenario-catalog.json"
        self.guide_root = self.root / "guides"
        self.guide_root.mkdir()
        self.catalog = {
            "contract": CATALOG_CONTRACT,
            "scenarios": [
                {
                    "id": "authorized-baseline",
                    "label": "Authorized baseline",
                    "status": "unavailable",
                    "evidenceGates": ["runIdentityMatches"],
                }
            ],
        }

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_valid_catalog_is_loaded(self) -> None:
        self.catalog_path.write_text(json.dumps(self.catalog), encoding="utf-8")

        scenarios = load_scenario_catalog(self.catalog_path, self.root, self.guide_root)

        self.assertEqual([row["id"] for row in scenarios], ["authorized-baseline"])

    def test_duplicate_nested_identity_keys_are_rejected(self) -> None:
        self.catalog_path.write_text(
            '{"contract":"damn-vulnerable-drone.antoine-scenario-catalog",'
            '"scenarios":[{"id":"old","id":"authorized-baseline",'
            '"label":"Baseline","status":"active","evidenceGates":["runIdentityMatches"]}]}'
        )

        with self.assertRaisesRegex(ValueError, "cannot read scenario catalog"):
            load_scenario_catalog(self.catalog_path, self.root, self.guide_root)

    def test_overflowing_numbers_are_rejected_even_in_unused_fields(self) -> None:
        self.catalog_path.write_text(
            json.dumps(self.catalog).replace('"contract"', '"unused":1e999,"contract"'),
            encoding="utf-8",
        )

        with self.assertRaisesRegex(ValueError, "cannot read scenario catalog"):
            load_scenario_catalog(self.catalog_path, self.root, self.guide_root)


if __name__ == "__main__":
    unittest.main()
