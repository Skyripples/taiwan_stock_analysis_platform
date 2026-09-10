from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class NavigationArchitectureTests(unittest.TestCase):
    def read(self, name: str) -> str:
        return (ROOT / name).read_text(encoding="utf-8")

    def test_home_has_exactly_seven_category_entries(self):
        html = self.read("index.html")
        for label, href in (
            ("市場", "market.html"), ("股票", "stocks.html"),
            ("期貨", "futures.html"), ("基金", "funds.html"),
            ("債券", "bonds.html"), ("外匯", "forex.html"),
            ("定存", "deposits.html"),
        ):
            self.assertIn(f'href="./{href}"', html)
            self.assertIn(f"<h3>{label}</h3>", html)
        self.assertEqual(html.count("category-card"), 7)
        self.assertEqual(html.count("data-category="), 7)
        self.assertNotIn("進入分類", html)
        self.assertEqual(html.count("status status-available"), 7)

    def test_sidebar_defines_hierarchy_and_existing_links(self):
        script = self.read("sidebar.js")
        for hub in ("market", "stocks", "futures", "funds", "bonds", "forex", "deposits"):
            self.assertIn(f'./{hub}.html', script)
        for page in ("market-overview.html", "chips-analysis.html", "calendar.html", "prediction.html", "stock-analysis.html", "screener.html"):
            self.assertIn(page, script)
        self.assertIn("全球市場", script)
        self.assertIn("sidebar-group-toggle", script)
        self.assertIn("aria-expanded", script)
        self.assertIn("taiwan_stock_navigation_groups", script)
        self.assertIn("navigationState[group.key] = isOpen", script)
        self.assertNotIn("querySelectorAll(\".sidebar-group.is-open\")", script)

    def test_market_hub_links_existing_market_features(self):
        html = self.read("market.html")
        for label in ("台股市場摘要", "法人／外資", "全球市場", "市場事件", "大盤預測"):
            self.assertIn(label, html)
        for page in ("market-overview.html", "chips-analysis.html", "calendar.html", "prediction.html"):
            self.assertIn(page, html)

    def test_stocks_hub_links_search_analysis_and_screener(self):
        html = self.read("stocks.html")
        for label in ("股票搜尋", "個股分析", "條件選股"):
            self.assertIn(label, html)
        self.assertIn("stock-analysis.html", html)
        self.assertIn("screener.html", html)

    def test_all_product_hubs_have_formal_content(self):
        expected = {
            "futures.html": "台指期 TX",
            "funds.html": "ETF 比較",
            "bonds.html": "政府公債殖利率比較",
            "forex.html": "主要貨幣對 TWD 匯率",
            "deposits.html": "銀行存款牌告利率比較",
        }
        for name, content in expected.items():
            html = self.read(name)
            self.assertIn(content, html)
            self.assertNotIn("未來功能區域", html)
            self.assertIn("product-integration.js", html)

    def test_product_summary_integration_covers_seven_categories(self):
        home = self.read("index.html")
        script = self.read("product-integration.js")
        for category in ("market", "stocks", "futures", "funds", "bonds", "forex", "deposits"):
            self.assertIn(f'data-product-summary="{category}"', home)
            self.assertIn(f'{category}:', script)
        for state in ("is-loading", "is-ready", "is-stale", "is-error", "is-empty"):
            self.assertIn(state, self.read("product-integration.css") + script)
        self.assertIn("返回分類總覽", script)

    def test_deposits_hub_is_now_bank_rate_comparison(self):
        html = self.read("deposits.html")
        self.assertIn("銀行存款牌告利率比較", html)
        self.assertNotIn("未來功能區域", html)

    def test_forex_hub_is_now_exchange_rate_page(self):
        html = self.read("forex.html")
        self.assertIn("主要貨幣對 TWD 匯率", html)
        self.assertNotIn("未來功能區域", html)

    def test_bonds_hub_is_now_government_yield_page(self):
        html = self.read("bonds.html")
        self.assertIn("政府公債殖利率比較", html)
        self.assertNotIn("未來功能區域", html)


if __name__ == "__main__":
    unittest.main()
