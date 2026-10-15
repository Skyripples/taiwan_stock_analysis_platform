from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class FuturesFrontendSourceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.integration = (ROOT / "product-integration.js").read_text(encoding="utf-8")
        cls.dashboard = (ROOT / "futures.js").read_text(encoding="utf-8")
        cls.html = (ROOT / "futures.html").read_text(encoding="utf-8")
        cls.css = (ROOT / "futures.css").read_text(encoding="utf-8")

    def test_summary_and_context_use_production_api(self):
        self.assertIn("`${globalThis.PlatformApiBase}/futures/summary`", self.integration)
        self.assertNotIn("data/futures/tx_futures.json", self.integration)
        self.assertIn("cache:\"no-store\"", self.integration)

    def test_latest_date_and_tx_front_contract_are_mapped(self):
        self.assertIn("date=p.latest_trade_date", self.integration)
        self.assertIn('row.product==="TX"&&row.contract_rank==="front"', self.integration)
        self.assertIn("f?.contract", self.integration)

    def test_dashboard_reuses_product_request_cache(self):
        self.assertIn("globalThis.PlatformProductDataGet=get", self.integration)
        self.assertIn("globalThis.PlatformProductDataGet(url)", self.dashboard)
        self.assertLess(self.html.index("stock-data-service.js"), self.html.index("product-integration.js"))

    def test_api_failure_is_visible_without_static_fallback(self):
        self.assertIn("目前無法取得資料", self.dashboard)
        self.assertNotIn("tx_futures.json", self.dashboard)

    def test_product_order_is_tx_mtx_tmf_everywhere(self):
        self.assertIn("const PRODUCT_ORDER = ['TX', 'MTX', 'TMF']", self.dashboard)
        self.assertIn("PRODUCT_ORDER.forEach(renderCard)", self.dashboard)
        self.assertIn("productRank(left.product) - productRank(right.product)", self.dashboard)
        self.assertLess(self.html.index('data-product="TX"'), self.html.index('data-product="MTX"'))
        self.assertLess(self.html.index('data-product="MTX"'), self.html.index('data-product="TMF"'))

    def test_chart_empty_message_stays_hidden_when_chart_has_data(self):
        self.assertIn(".chart-wrap p[hidden]{display:none}", self.css)
        self.assertIn("empty.hidden = points.length > 1", self.dashboard)


if __name__ == "__main__":
    unittest.main()
