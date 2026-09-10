from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import update_bond_yields as bonds


TAIWAN = {
    "data_date": "2026-09-09", "source": bonds.TPEX_PAGE_URL,
    "yields": [
        {"tenor": "2Y", "tenor_months": 24, "yield": 1.7, "source_date": "2026-09-09"},
        {"tenor": "10Y", "tenor_months": 120, "yield": 1.9, "source_date": "2026-09-09"},
    ],
}
UNITED_STATES = {
    "data_date": "2026-09-09", "source": bonds.TREASURY_URL,
    "yields": [
        {"tenor": "2Y", "tenor_months": 24, "yield": 4.4, "source_date": "2026-09-09"},
        {"tenor": "10Y", "tenor_months": 120, "yield": 4.8, "source_date": "2026-09-09"},
    ],
}


class BondYieldCollectorTests(unittest.TestCase):
    def test_schema_and_spread(self):
        payload = bonds.build_payload(TAIWAN, UNITED_STATES, datetime(2026, 9, 10, tzinfo=timezone.utc))
        bonds.validate(payload)
        self.assertAlmostEqual(payload["markets"]["taiwan"]["spread_10y_2y"], 0.2)
        self.assertAlmostEqual(payload["markets"]["united_states"]["spread_10y_2y"], 0.4)

    def test_source_failure_preserves_existing_output(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "bond_yields.json"
            original = '{"valid": true}\n'
            output.write_text(original, encoding="utf-8")
            with patch.object(bonds, "OUTPUT", output), patch.object(bonds, "fetch_taiwan", side_effect=RuntimeError("offline")):
                self.assertEqual(bonds.main(), 1)
            self.assertEqual(output.read_text(encoding="utf-8"), original)

    def test_current_json_schema(self):
        payload = json.loads((ROOT / "data" / "bonds" / "bond_yields.json").read_text(encoding="utf-8"))
        bonds.validate(payload)
        self.assertTrue(payload["markets"]["taiwan"]["data_date"])
        self.assertTrue(payload["markets"]["united_states"]["data_date"])


class BondPageSmokeTests(unittest.TestCase):
    def test_page_has_market_switch_sort_curve_and_sources(self):
        html = (ROOT / "bonds.html").read_text(encoding="utf-8")
        script = (ROOT / "bonds.js").read_text(encoding="utf-8")
        for value in ("政府公債殖利率比較", 'data-market="taiwan"', 'data-market="united_states"', "bondTable", "yieldCurve", "yieldSpread"):
            self.assertIn(value, html)
        self.assertIn("證券櫃檯買賣中心", html)
        self.assertIn("U.S. Treasury", html)
        self.assertIn('button.addEventListener("click"', script)
        self.assertIn('header.addEventListener("click"', script)
        self.assertIn("renderCurve", script)
        self.assertIn("bond_yields.json", script)


if __name__ == "__main__":
    unittest.main()
