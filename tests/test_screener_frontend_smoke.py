from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ScreenerFrontendSmokeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = (ROOT / "screener.html").read_text(encoding="utf-8")
        cls.script = (ROOT / "screener.js").read_text(encoding="utf-8")
        cls.service = (ROOT / "stock-data-service.js").read_text(encoding="utf-8")

    def test_page_and_all_filters_exist(self):
        self.assertIn("條件選股", self.html)
        for name in (
            "search", "market", "industry", "instrument_type", "price_min", "price_max",
            "change_percent_min", "change_percent_max", "pe_min", "pe_max", "pb_min", "pb_max",
            "dividend_yield_min", "dividend_yield_max", "revenue_yoy_min", "revenue_yoy_max",
            "roe_min", "roe_max", "debt_ratio_min", "debt_ratio_max", "foreign_5d_min", "foreign_5d_max",
        ):
            self.assertIn(f'name="{name}"', self.html)

    def test_submit_reset_sort_and_pagination_handlers_exist(self):
        for token in ("addEventListener('submit'", "addEventListener('reset'", "previous.addEventListener", "next.addEventListener", "th[data-sort]"):
            self.assertIn(token, self.script)

    def test_empty_and_api_error_states_are_isolated(self):
        self.assertIn("沒有符合目前條件的股票", self.script)
        self.assertIn("目前無法取得資料", self.script)
        self.assertIn("stock-analysis.html?symbol=", self.script)

    def test_data_service_has_centralized_endpoints(self):
        self.assertIn("async screenStocks", self.service)
        self.assertIn("async getScreenerOptions", self.service)
        self.assertEqual(self.script.count("172-238-20-217"), 0)


if __name__ == "__main__":
    unittest.main()
