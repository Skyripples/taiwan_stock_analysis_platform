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
import update_exchange_rates as rates


FIXTURE = {
    "data": {
        "dataSets": [
            ["20260908", "32.000", "160.000", "1.350", "8.000", "1280.000", "1.400", "1.280", "6.400", "0.700", "0", "0", "0", "0", "1.125", "-", "-", "-", "0"],
            ["20260909", "32.100", "161.000", "1.360", "8.010", "1290.000", "1.410", "1.290", "6.410", "-", "0", "0", "0", "0", "1.130", "-", "-", "-", "0"],
        ]
    }
}


class ExchangeRateCollectorTests(unittest.TestCase):
    def test_normalize_cross_rates_and_schema(self):
        payload = rates.normalize(FIXTURE, datetime(2026, 9, 9, tzinfo=timezone.utc))
        rates.validate(payload)
        values = {item["currency"]: item["twd_rate"] for item in payload["rates"]}
        self.assertIsNone(payload["data_date"])
        self.assertEqual(payload["latest_source_date"], "2026-09-09")
        self.assertEqual(len(values), 10)
        self.assertAlmostEqual(values["USD"], 32.1)
        self.assertAlmostEqual(values["JPY"], 32.1 / 161)
        self.assertAlmostEqual(values["EUR"], 32.1 * 1.13)
        self.assertAlmostEqual(values["AUD"], 32.0 * 0.7)
        dates = {item["currency"]: item["source_date"] for item in payload["rates"]}
        self.assertEqual(dates["USD"], "2026-09-09")
        self.assertEqual(dates["AUD"], "2026-09-08")

    def test_source_failure_preserves_existing_output(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "exchange_rates.json"
            original = '{"valid": true}\n'
            output.write_text(original, encoding="utf-8")
            with patch.object(rates, "OUTPUT", output), patch.object(rates, "fetch", side_effect=RuntimeError("offline")):
                self.assertEqual(rates.main(), 1)
            self.assertEqual(output.read_text(encoding="utf-8"), original)

    def test_current_json_schema(self):
        payload = json.loads((ROOT / "data" / "forex" / "exchange_rates.json").read_text(encoding="utf-8"))
        rates.validate(payload)
        self.assertEqual({item["currency"] for item in payload["rates"]}, set(rates.CURRENCIES))


class ForexPageSmokeTests(unittest.TestCase):
    def test_page_has_search_sort_converter_and_source(self):
        html = (ROOT / "forex.html").read_text(encoding="utf-8")
        script = (ROOT / "forex.js").read_text(encoding="utf-8")
        for value in ("主要貨幣對 TWD 匯率", "currencySearch", "forexTable", "exchangeAmount", "fromCurrency", "toCurrency"):
            self.assertIn(value, html)
        self.assertIn("中央銀行統計資料庫", html)
        self.assertIn('header.addEventListener("click"', script)
        self.assertIn('search.addEventListener("input"', script)
        self.assertIn("value * fromRate / toRate", script)
        self.assertIn("exchange_rates.json", script)


if __name__ == "__main__":
    unittest.main()
