from __future__ import annotations

import json
import unittest
from pathlib import Path

from scripts.build_stock_industry_group_taxonomy import map_path


ROOT = Path(__file__).resolve().parents[1]


class TaxonomyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mapping = json.loads((ROOT / "config/stock_industry_group_mapping.json").read_text(encoding="utf-8"))
        cls.overrides = json.loads((ROOT / "config/stock_industry_group_overrides.json").read_text(encoding="utf-8"))["overrides"]

    def test_pcb_and_substrate_path_maps_to_multiple_groups(self):
        self.assertEqual(map_path("印刷電路板 > 硬板、軟板、IC載板製造", self.mapping), ["ic_substrate", "pcb"])

    def test_passive_components_map_to_parent_and_leaf(self):
        self.assertEqual(map_path("被動元件 > 電容器", self.mapping), ["capacitors", "passive_components"])

    def test_unknown_path_creates_no_group(self):
        self.assertEqual(map_path("未定義產業 > 未定義區段", self.mapping), [])

    def test_abf_is_curated_not_implied_by_ic_substrate(self):
        self.assertNotIn("abf_substrate", map_path("半導體 > 基板", self.mapping))
        self.assertEqual({item["symbol"] for item in self.overrides if item["group"] == "abf_substrate"}, {"3037", "8046", "3189"})
        self.assertTrue(all(item.get("evidence") for item in self.overrides))

    def test_static_output_is_idempotent_and_has_reverse_index(self):
        payload = json.loads((ROOT / "data/stocks/stock-groups.json").read_text(encoding="utf-8"))
        keys = [item["key"] for item in payload["by_symbol"]["3037"]["groups"]]
        self.assertEqual(len(keys), len(set(keys)))
        self.assertIn("3037", payload["by_group"]["abf_substrate"])
        self.assertEqual(payload["by_symbol"].get("0050", {"groups": []})["groups"], [])

    def test_existing_official_industry_is_untouched(self):
        source = (ROOT / "scripts/build_stock_industry_group_taxonomy.py").read_text(encoding="utf-8")
        self.assertNotIn('item["industry"] =', source)

    def test_migration_preserves_many_to_many_key_and_provenance(self):
        sql = (ROOT / "scripts/database/migrations/018_stock_industry_groups.sql").read_text(encoding="utf-8")
        self.assertIn("PRIMARY KEY (stock_id, group_id)", sql)
        self.assertIn("provenance text NOT NULL", sql)


if __name__ == "__main__":
    unittest.main()
