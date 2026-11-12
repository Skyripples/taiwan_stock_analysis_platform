from __future__ import annotations

import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class MlPredictionFactorScoringFrontendTests(unittest.TestCase):
    def test_page_has_independent_factor_direction_score_interface(self):
        page = (ROOT / "ml-prediction.html").read_text(encoding="utf-8")
        self.assertIn("市場因子與方向評分", page)
        self.assertIn('id="factorTaskSelect"', page)
        self.assertIn("US_PRE", page)
        self.assertIn("US_POST", page)

    def test_real_mode_does_not_fallback_to_mock_factor_values(self):
        script = (ROOT / "ml-prediction.js").read_text(encoding="utf-8")
        self.assertIn("data.factor_scoring?.tasks", script)
        self.assertIn("unavailableTask(key, data.data_mode === 'real')", script)
        self.assertIn("factorTaskSelect", script)

    def test_mock_values_are_explicit_and_weights_remain_unverified(self):
        payload = json.loads((ROOT / "mock-data" / "b4-ml-preview.json").read_text(encoding="utf-8"))
        factors = payload["factor_scoring"]
        self.assertEqual(factors["mode"], "mock")
        self.assertEqual(factors["tasks"]["DAY"]["summary"]["status"], "pending_rule")
        self.assertTrue(all(item["weight_status"] == "unverified" for item in factors["tasks"]["DAY"]["factors"]))
