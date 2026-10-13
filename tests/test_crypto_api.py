from __future__ import annotations

import sys
import unittest
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from api.routes.crypto import router
from api.services import crypto

UTC = timezone.utc


def rows(symbol="BTC-USD"):
    start = datetime(2026, 10, 12, tzinfo=UTC)
    return [{"symbol": symbol, "timestamp_utc": start + timedelta(minutes=5 * index),
             "open": Decimal("100"), "high": Decimal(str(102 + index)), "low": Decimal("99"),
             "close": Decimal(str(100 + index)), "volume": Decimal("2"),
             "source": "Coinbase Exchange", "fetched_at": start + timedelta(minutes=5 * index + 6)}
            for index in range(289)]


class FakeCursor:
    def __init__(self, result): self.result=result; self.query=None; self.parameters=None
    def __enter__(self): return self
    def __exit__(self, *_): return False
    def execute(self, query, parameters): self.query=query; self.parameters=parameters
    def fetchall(self): return self.result


class FakeConnection:
    def __init__(self, cursor): self._cursor=cursor
    def cursor(self): return self._cursor


class FakePool:
    def __init__(self, result): self.cursor=FakeCursor(result)
    @contextmanager
    def connection(self): yield FakeConnection(self.cursor)


class CryptoApiTests(unittest.TestCase):
    def setUp(self):
        app=FastAPI(); app.include_router(router,prefix="/api/v1"); self.client=TestClient(app)

    def test_btc_and_eth_summary_metrics_and_stale(self):
        latest=rows()[-1]["timestamp_utc"]
        btc=crypto.summarize_candles("BTC-USD",rows(),now=latest+timedelta(minutes=16))
        eth=crypto.summarize_candles("ETH-USD",rows("ETH-USD"),now=latest+timedelta(minutes=6))
        self.assertAlmostEqual(btc["change_1h"],(388/376-1)*100)
        self.assertAlmostEqual(btc["change_4h"],(388/340-1)*100)
        self.assertAlmostEqual(btc["change_24h"],(388/100-1)*100)
        self.assertEqual(btc["high_24h"],390.0); self.assertEqual(btc["low_24h"],99.0)
        self.assertEqual(btc["volume_24h"],576.0); self.assertTrue(btc["stale"]); self.assertFalse(eth["stale"])
        self.assertTrue(btc["latest_timestamp"].endswith("+00:00"))

    def test_empty_summary_is_safe(self):
        result=crypto.summarize_candles("BTC-USD",[])
        self.assertIsNone(result["close"]); self.assertIsNone(result["stale"])

    def test_five_minute_range_query_is_parameterized(self):
        moment=datetime(2026,10,13,tzinfo=UTC)
        fake=FakePool([{"timestamp_utc":moment,"open":Decimal("1"),"high":Decimal("2"),"low":Decimal("1"),"close":Decimal("2"),"volume":Decimal("3")}])
        with patch.object(crypto,"pool",fake): result=crypto.get_crypto_candles("BTC-USD","1H")
        self.assertEqual(result["interval_seconds"],300); self.assertEqual(result["count"],1)
        self.assertEqual(result["candles"][0]["timestamp"],moment.isoformat())
        self.assertEqual(fake.cursor.parameters,("BTC-USD",300,300,timedelta(hours=1)))
        self.assertIn("%s",fake.cursor.query)

    def test_routes_and_validation(self):
        summary={"interval_seconds":300,"results":[]}
        with patch("api.routes.crypto.get_crypto_summary",return_value=summary):
            self.assertEqual(self.client.get("/api/v1/crypto/summary").json(),summary)
        with patch("api.routes.crypto.get_crypto_candles",return_value={"symbol":"ETH-USD","range":"7D","candles":[]}):
            response=self.client.get("/api/v1/crypto/ETH-USD/candles?range=7D")
            self.assertEqual(response.status_code,200); self.assertEqual(response.json()["candles"],[])
        self.assertEqual(self.client.get("/api/v1/crypto/DOGE-USD/candles?range=24H").status_code,422)
        self.assertEqual(self.client.get("/api/v1/crypto/BTC-USD/candles?range=ALL").status_code,422)


if __name__=="__main__": unittest.main()
