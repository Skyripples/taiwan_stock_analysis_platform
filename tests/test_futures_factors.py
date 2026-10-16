from __future__ import annotations

import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class FuturesFactorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config = json.loads((ROOT / "config/futures_factor_config.json").read_text(encoding="utf-8"))

    def test_config_has_exactly_ten_unique_factors_and_weight_total(self):
        factors = self.config["factors"]
        self.assertEqual(len(factors), 10)
        self.assertEqual(len({item["factor_id"] for item in factors}), 10)
        self.assertAlmostEqual(sum(item["initial_weight"] for item in factors), 1.0)

    def test_initial_weight_contract(self):
        required = {"factor_id", "label", "category", "initial_weight", "model_weight", "effective_weight", "weight_source", "feature_type", "relation", "data_source", "status", "latest_value", "latest_change", "observed_at", "available_at", "source_date", "fetched_at", "model_version"}
        for factor in self.config["factors"]:
            self.assertTrue(required.issubset(factor))
            self.assertIsNone(factor["model_weight"])
            self.assertIsNone(factor["model_version"])
            self.assertEqual(factor["weight_source"], "manual_initial")
            self.assertEqual(factor["effective_weight"], factor["initial_weight"])

    def test_macro_factors_are_unavailable_and_have_no_fake_values(self):
        for factor in self.config["factors"]:
            if factor["factor_id"] in {"CPI", "NFP", "FOMC"}:
                self.assertEqual(factor["feature_type"], "surprise")
                self.assertEqual(factor["status"], "unavailable")
                self.assertIsNone(factor["latest_value"])
                self.assertIsNone(factor["data_source"])

    def test_existing_market_factor_config_is_untouched_and_ui_is_objective(self):
        self.assertTrue((ROOT / "config/factor_config.json").exists())
        html = (ROOT / "futures.html").read_text(encoding="utf-8")
        script = (ROOT / "futures-factors.js").read_text(encoding="utf-8")
        self.assertIn("市場影響因子", html)
        self.assertIn("futuresFactors", html)
        self.assertNotIn("Buy", html + script)
        self.assertNotIn("Sell", html + script)
        self.assertIn("PlatformXYChart", (ROOT / "futures.js").read_text(encoding="utf-8"))
        self.assertIn("chartEmpty", html)

    def test_additive_future_storage_keeps_point_in_time_metadata(self):
        migration = (ROOT / "scripts/database/migrations/012_futures_factor_foundation.sql").read_text(encoding="utf-8")
        for token in ("market_factor_series", "macro_events", "observed_at", "available_at", "source_date", "release_time"):
            self.assertIn(token, migration)


if __name__ == "__main__":
    unittest.main()
