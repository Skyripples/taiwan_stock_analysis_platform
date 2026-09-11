from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PAGES = (
    "index.html", "market.html", "stocks.html", "futures.html", "funds.html",
    "bonds.html", "forex.html", "deposits.html", "market-overview.html",
    "chips-analysis.html", "calendar.html", "prediction.html",
    "stock-analysis.html", "screener.html",
)


class ProductionUxPolishTests(unittest.TestCase):
    def read(self, name: str) -> str:
        return (ROOT / name).read_text(encoding="utf-8")

    def test_page_titles_use_platform_name(self):
        for name in PAGES:
            html = self.read(name)
            title = re.search(r"<title>(.*?)</title>", html, re.S)
            self.assertIsNotNone(title, name)
            self.assertIn("台股投資分析平台", title.group(1), name)

    def test_local_page_assets_and_links_exist(self):
        for name in PAGES:
            html = self.read(name)
            for target in re.findall(r'(?:href|src)="\./([^"?#]+)', html):
                self.assertTrue((ROOT / target).exists(), f"{name}: missing {target}")

    def test_no_duplicate_local_styles_or_scripts(self):
        for name in PAGES:
            html = self.read(name)
            assets = re.findall(r'(?:href|src)="(\./[^"?#]+\.(?:css|js))', html)
            self.assertEqual(len(assets), len(set(assets)), name)

    def test_shared_polish_has_mobile_focus_and_scroll_support(self):
        css = self.read("ux-polish.css")
        script = self.read("sidebar.js")
        self.assertIn("max-width: 430px", css)
        self.assertIn(":focus-visible", css)
        self.assertIn("overflow-wrap: anywhere", css)
        self.assertIn("page-breadcrumb", script)
        self.assertIn('aria-label", "麵包屑導覽', script)
        self.assertIn("PlatformUI", script)

    def test_data_limitations_are_user_visible(self):
        self.assertIn("匯率（參考）", self.read("forex.html"))
        self.assertIn("銀行牌告利率", self.read("deposits.html"))
        self.assertIn("最近完成交易日行情", self.read("futures.html"))
        self.assertIn("不代表未來保證", self.read("prediction.js"))
        self.assertNotIn("即時 API", self.read("stock-analysis.html") + self.read("stock-analysis.js"))

    def test_sidebar_accessibility_state_is_kept(self):
        script = self.read("sidebar.js")
        self.assertIn('toggle.setAttribute("aria-expanded"', script)
        self.assertIn('sidebar.setAttribute("aria-label", "平台導覽")', script)
        self.assertIn('status.setAttribute("aria-live", "polite")', script)


if __name__ == "__main__":
    unittest.main()
