from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


class XYChartUiTests(unittest.TestCase):
    def test_shared_chart_has_grid_ticks_and_axis_titles(self):
        script = (ROOT / "xy-chart.js").read_text(encoding="utf-8")
        css = (ROOT / "xy-chart.css").read_text(encoding="utf-8")
        for marker in ("xy-grid", "xy-tick", "xy-axis-title", "PlatformXYChart"):
            self.assertIn(marker, script + css)

    def test_product_charts_use_shared_renderer(self):
        for page, script_name in (("crypto.html", "crypto.js"), ("futures.html", "futures.js"), ("bonds.html", "bonds.js"), ("stock-analysis.html", "stock-analysis.js"), ("chips-analysis.html", "chips-analysis.js")):
            html = (ROOT / page).read_text(encoding="utf-8")
            script = (ROOT / script_name).read_text(encoding="utf-8")
            self.assertIn("xy-chart.css", html)
            self.assertIn("xy-chart.js", html)
            self.assertIn("PlatformXYChart", script)

    def test_stock_historical_research_tabs_are_lazy_and_isolated(self):
        html = (ROOT / "stock-analysis.html").read_text(encoding="utf-8")
        script = (ROOT / "stock-analysis.js").read_text(encoding="utf-8")
        service = (ROOT / "stock-data-service.js").read_text(encoding="utf-8")
        methods = {"revenue": "getRevenueHistory", "financials": "getFinancials", "valuation": "getValuationHistory", "chips": "getChips"}
        for dataset, label in (("revenue", "月營收"), ("financials", "財務"), ("valuation", "估值"), ("chips", "籌碼")):
            self.assertIn(f'data-history-tab="{dataset}"', html)
            self.assertIn(label, html)
            self.assertIn(methods[dataset], script)
        self.assertIn("historyCache", script)
        self.assertIn("Promise.allSettled([dataService.getIndustryPeers", script)
        self.assertNotIn("Promise.allSettled([\n        profile.instrument_type", script)
        self.assertIn("其他頁籤仍可使用", script)
        self.assertIn("'—'", script)
        self.assertIn("getRevenueHistory", service)
        self.assertIn("getValuationHistory", service)

    def test_home_loads_api_base_before_product_summaries(self):
        html = (ROOT / "index.html").read_text(encoding="utf-8")
        service = html.index('<script src="./stock-data-service.js"></script>')
        integration = html.index('<script src="./product-integration.js"></script>')
        self.assertLess(service, integration)


if __name__ == "__main__":
    unittest.main()
