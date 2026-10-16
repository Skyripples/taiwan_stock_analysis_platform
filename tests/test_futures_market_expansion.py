from __future__ import annotations

import json
import subprocess
import sys
import unittest
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from futures_market import basis, contract_metadata, historical_percentile, price_oi_state
from update_tx_futures import select_contracts


def quote(product, month, close="22000"):
    return [product, month, "21900", "22100", "21800", close, "+100", "+0.46%", "10", "20", "30", "21950", "40000", "0", "0", "0", "0"]


class FuturesMarketExpansionTests(unittest.TestCase):
    def test_schema_and_products(self):
        migration = (ROOT / "scripts/database/migrations/008_futures_market_foundation.sql").read_text(encoding="utf-8")
        for token in ("futures_daily_prices", "available_at", "after_hours"):
            self.assertIn(token, migration)
        for product in ("TX", "MTX", "TMF"):
            selected = select_contracts([quote(product, "202610")], date(2026, 10, 1), product=product)
            self.assertEqual(selected[0]["contract_code"], "202610")

    def test_derived_values(self):
        self.assertEqual(basis(22000, 21900), (100.0, 100 / 21900 * 100))
        self.assertIsNone(historical_percentile([1] * 19, 1))
        self.assertEqual(price_oi_state(1, -1), "價格上漲 / OI 減少")
        self.assertEqual(contract_metadata(date(2026, 10, 14), date(2026, 10, 21))["days_to_expiry"], 7)

    def test_backfill_and_import_dry_run(self):
        command = [sys.executable, str(ROOT / "scripts/backfill_futures_history.py"), "--products", "TX", "MTX", "TMF", "--start", "2026-10-01", "--end", "2026-10-12", "--dry-run"]
        payload = json.loads(subprocess.run(command, cwd=ROOT, text=True, capture_output=True, check=True).stdout)
        self.assertEqual(payload["network_requests"], 0)
        imported = json.loads(subprocess.run([sys.executable, str(ROOT / "scripts/import_futures_history.py"), "--dry-run"], cwd=ROOT, text=True, capture_output=True, check=True).stdout)
        self.assertEqual(imported["duplicates"], 0)

    def test_dashboard_and_api(self):
        html = (ROOT / "futures.html").read_text(encoding="utf-8")
        js = (ROOT / "futures.js").read_text(encoding="utf-8")
        for token in ("TMF 微型臺指", "5 分鐘行情", "契約結構"):
            self.assertIn(token, html)
        for removed in ("期現貨價差", "價格 × 未平倉量", "<h2>尚未提供</h2>"):
            self.assertNotIn(removed, html)
        self.assertIn("/futures/summary", js)
        self.assertIn("/futures/history", js)
        self.assertIn("/futures/intraday", js)


if __name__ == "__main__":
    unittest.main()
