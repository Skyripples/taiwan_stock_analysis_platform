from __future__ import annotations

import sys
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

import requests


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from providers.taiwan_market_provider import TaiwanMarketOverviewProvider


class FakeResponse:
    def __init__(self, payload=None, status=200):
        self.payload = payload
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}", response=self)

    def json(self):
        return self.payload


class TaiwanMarketProviderTests(unittest.TestCase):
    target = date(2026, 9, 1)

    def setUp(self):
        self.provider = TaiwanMarketOverviewProvider()
        self.provider.tpex_backoff_seconds = (0, 0)

    @patch("providers.taiwan_market_provider.time.sleep")
    @patch("providers.taiwan_market_provider.requests.get")
    def test_primary_retries_transient_520(self, get, sleep):
        get.side_effect = [
            FakeResponse(status=520),
            FakeResponse([{"Date": "1150901"}]),
        ]
        payload = self.provider._get_tpex_json(self.provider.tpex_highlight_url)
        self.assertEqual(payload[0]["Date"], "1150901")
        self.assertEqual(get.call_count, 2)
        self.assertEqual(sleep.call_count, 1)

    @patch("providers.taiwan_market_provider.time.sleep")
    @patch("providers.taiwan_market_provider.requests.get")
    def test_official_fallback_requires_exact_target_date(self, get, _sleep):
        index_payload = {
            "stat": "ok",
            "date": "20260901",
            "tables": [{"data": [["115/09/01", "1", "2", "3", 410.77, 9.07]]}],
        }
        quotes_payload = {
            "stat": "ok",
            "date": "20260901",
            "tables": [
                {
                    "totalTradingAmount": "253,850,004,893",
                    "data": [
                        ["6488", "A", "100", "1.0"],
                        ["8069", "B", "100", "-2.0"],
                        ["8299", "C", "100", "0.0"],
                        ["0050", "ETF", "100", "3.0"],
                        ["12345", "Warrant", "100", "3.0"],
                    ],
                }
            ],
        }
        get.side_effect = [
            FakeResponse(status=520),
            FakeResponse(status=520),
            FakeResponse(status=520),
            FakeResponse(index_payload),
            FakeResponse(quotes_payload),
        ]
        row, sources = self.provider._fetch_tpex_for_date(self.target)
        self.assertEqual(row["Date"], "1150901")
        self.assertEqual(row["DailyTradingValue"], "253850")
        self.assertEqual(row["PriceRiseCompanyNumbers"], "1")
        self.assertEqual(row["PriceDeclineCompanyNumbers"], "1")
        self.assertEqual(row["PriceFlatCompanyNumbers"], "1")
        self.assertEqual(sources, [self.provider.tpex_index_url, self.provider.tpex_quotes_url])

    @patch("providers.taiwan_market_provider.time.sleep")
    @patch("providers.taiwan_market_provider.requests.get")
    def test_date_mismatch_cannot_be_accepted_as_latest(self, get, _sleep):
        get.side_effect = [
            FakeResponse([{"Date": "1150831"}]),
            FakeResponse({"stat": "ok", "date": "20260831", "tables": []}),
        ]
        with self.assertRaisesRegex(ValueError, "All official TPEx sources failed"):
            self.provider._fetch_tpex_for_date(self.target)


if __name__ == "__main__":
    unittest.main()
