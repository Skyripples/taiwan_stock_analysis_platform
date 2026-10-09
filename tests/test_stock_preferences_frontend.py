from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class StockPreferencesFrontendTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.preferences = (ROOT / "stock-preferences.js").read_text(encoding="utf-8")
        cls.screener = (ROOT / "screener.js").read_text(encoding="utf-8")
        cls.stock = (ROOT / "stock-analysis.js").read_text(encoding="utf-8")
        cls.sidebar = (ROOT / "sidebar.js").read_text(encoding="utf-8")

    def test_versioned_project_specific_keys_and_validation(self):
        self.assertIn("taiwan_stock_analysis_platform:v1", self.preferences)
        self.assertIn("cleanWatchlist", self.preferences)
        self.assertIn("cleanScreener", self.preferences)
        self.assertIn("schema !== 1", self.preferences)

    def test_storage_failure_and_cross_tab_support(self):
        self.assertIn("目前僅於本頁暫存", self.preferences)
        self.assertIn("設定保存在此瀏覽器，不會跨裝置同步", self.preferences)
        self.assertIn("addEventListener?.('storage'", self.preferences)

    def test_screener_restores_after_options_and_prevents_stale_response(self):
        self.assertIn("await loadOptions(); restoreSettings(); await load()", self.screener)
        self.assertIn("const requestId = ++state.requestId", self.screener)
        self.assertGreaterEqual(self.screener.count("requestId !== state.requestId"), 2)
        self.assertIn("clearScreenerSettings", self.screener)

    def test_watchlist_buttons_do_not_trigger_row_navigation(self):
        self.assertIn("event.stopPropagation()", self.screener)
        self.assertIn("event.target.closest('a,button,input,select')", self.screener)
        self.assertIn("data-watchlist-symbol", self.screener)
        self.assertIn("stockWatchlistToggle", self.stock)

    def test_stock_pages_are_public_without_admin_access(self):
        self.assertIn('const publicFeatures = new Set(["stock_analysis"])', self.sidebar)
        self.assertIn('const publicCategories = new Set(["stocks"])', self.sidebar)


if __name__ == "__main__":
    unittest.main()
