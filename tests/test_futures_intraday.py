from __future__ import annotations

import sys
import io
import zipfile
import unittest
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from futures_intraday import CandleAggregator, FuturesTick, bucket_start, resolve_session
from futures_intraday_reconstruction import aggregate_ticks
from providers.taifex_tick_csv_provider import iter_product_ticks, official_url
from api.routes.futures import router
from api.services import futures as futures_service


def tick(value: str, price: str, volume: int, total: int, product="TX", contract="TX202610"):
    return FuturesTick(datetime.fromisoformat(value), product, contract, Decimal(price), volume, total)


class FakeCursor:
    def __init__(self, rows): self.rows, self.parameters = rows, None
    def __enter__(self): return self
    def __exit__(self, *_): return False
    def execute(self, _query, parameters): self.parameters = parameters
    def fetchall(self): return self.rows


class FakeConnection:
    def __init__(self, cursor): self._cursor = cursor
    def cursor(self): return self._cursor


class FakePool:
    def __init__(self, rows): self.cursor = FakeCursor(rows)
    @contextmanager
    def connection(self): yield FakeConnection(self.cursor)


class FuturesIntradayTests(unittest.TestCase):
    def test_tick_to_completed_5m_ohlcv_and_boundary(self):
        aggregator = CandleAggregator()
        self.assertEqual(bucket_start(datetime.fromisoformat("2026-10-15T08:47:59+08:00")).minute, 45)
        self.assertEqual(aggregator.add(tick("2026-10-15T08:46:00+08:00", "47000", 2, 100)), [])
        aggregator.add(tick("2026-10-15T08:48:00+08:00", "47020", 3, 103))
        aggregator.add(tick("2026-10-15T08:49:00+08:00", "46990", 1, 104))
        completed = aggregator.add(tick("2026-10-15T08:50:00+08:00", "47010", 2, 106))
        self.assertEqual(len(completed), 1)
        candle = completed[0]
        self.assertEqual((candle.open, candle.high, candle.low, candle.close),
                         tuple(map(Decimal, ("47000", "47020", "46990", "46990"))))
        self.assertEqual(candle.volume, 6)
        self.assertEqual(candle.timestamp_utc.minute, 45)

    def test_duplicate_cumulative_tick_does_not_duplicate_volume(self):
        aggregator = CandleAggregator()
        aggregator.add(tick("2026-10-15T08:46:00+08:00", "47000", 2, 100))
        aggregator.add(tick("2026-10-15T08:46:01+08:00", "47000", 2, 100))
        candle = aggregator.flush_completed(datetime(2026, 10, 15, 1, 0, tzinfo=timezone.utc))[0]
        self.assertEqual(candle.volume, 2)

    def test_regular_after_hours_and_cross_midnight_trading_date(self):
        self.assertEqual(resolve_session(datetime.fromisoformat("2026-09-04T10:00:00+08:00")),
                         ("regular", datetime(2026, 9, 4).date()))
        self.assertEqual(resolve_session(datetime.fromisoformat("2026-09-04T16:00:00+08:00")),
                         ("after_hours", datetime(2026, 9, 7).date()))
        self.assertEqual(resolve_session(datetime.fromisoformat("2026-09-07T02:00:00+08:00")),
                         ("after_hours", datetime(2026, 9, 7).date()))

    def test_contract_and_product_are_preserved_across_roll(self):
        aggregator = CandleAggregator()
        aggregator.add(tick("2026-10-15T08:46:00+08:00", "47000", 1, 1, contract="TX202610"))
        aggregator.add(tick("2026-10-15T08:46:00+08:00", "47100", 1, 1, contract="TX202611"))
        candles = aggregator.flush_completed(datetime(2026, 10, 15, 1, 0, tzinfo=timezone.utc))
        self.assertEqual({c.contract for c in candles}, {"TX202610", "TX202611"})

    def test_null_volume_is_not_invented(self):
        aggregator = CandleAggregator()
        with self.assertRaisesRegex(ValueError, "volume is unavailable"):
            aggregator.add(tick("2026-10-15T08:46:00+08:00", "47000", None, None))

    def test_schema_and_upsert_are_additive_and_idempotent(self):
        migration = (ROOT / "scripts/database/migrations/011_futures_intraday_expansion.sql").read_text(encoding="utf-8")
        collector = (ROOT / "scripts/futures_intraday_reconstruction.py").read_text(encoding="utf-8")
        self.assertIn("ADD COLUMN IF NOT EXISTS trading_date", migration)
        self.assertIn("ON CONFLICT (product,contract,session,timestamp_utc,interval_seconds)", collector)

    def test_official_csv_parse_filter_session_and_ohlcv(self):
        header = "成交日期,商品代號,到期月份(週別),成交時間,成交價格,成交數量(B+S),近月價格,遠月價格,開盤集合競價\n"
        body = "20261015,TX,202610,151001,100,2,-,-,\n20261015,TX,202610,151002,102,3,-,-,\n20261016,TX,202610,084501,101,4,-,-,\n20261016,MTX,202610,084501,99,1,-,-,\n20261016,ABC,202610,084501,1,1,-,-,\n"
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("Daily.csv", (header + body).encode("utf-8-sig"))
        ticks = list(iter_product_ticks(buffer.getvalue()))
        self.assertEqual([item.product for item in ticks], ["TX", "TX", "TX", "MTX"])
        candles = aggregate_ticks(ticks, datetime(2026, 10, 16).date(), available_at=datetime.now(timezone.utc))
        night = next(item for item in candles if item.session == "after_hours")
        self.assertEqual((night.open, night.high, night.low, night.close, night.volume),
                         (Decimal("100"), Decimal("102"), Decimal("100"), Decimal("102"), 5))
        self.assertTrue(official_url(datetime(2026, 10, 16).date()).endswith("Daily_2026_10_16.zip"))

    def test_api_ranges_stale_and_frontend_source(self):
        service = (ROOT / "scripts/api/services/futures.py").read_text(encoding="utf-8")
        route = (ROOT / "scripts/api/routes/futures.py").read_text(encoding="utf-8")
        html = (ROOT / "futures.html").read_text(encoding="utf-8")
        js = (ROOT / "futures.js").read_text(encoding="utf-8")
        self.assertIn('"1H": timedelta(hours=1)', service)
        self.assertIn('"7D": timedelta(days=7)', service)
        self.assertIn('update_mode": "daily_official_reconstruction', service)
        self.assertIn("contract_rank='front'", service)
        self.assertIn('@router.get("/intraday")', route)
        self.assertIn("/futures/intraday", js)
        self.assertIn("每日收盤後依期交所官方逐筆成交資料重建", html)
        self.assertNotIn("Live｜5 分鐘行情", js)

    def test_api_1h_4h_1d_ranges_and_validation(self):
        moment = datetime.now(timezone.utc)
        rows = [{"product": "TX", "contract": "TX202610", "session": "regular",
                 "timestamp": moment, "trading_date": moment.date(), "open": Decimal("1"),
                 "high": Decimal("2"), "low": Decimal("1"), "close": Decimal("2"),
                 "volume": 3, "source": "TAIFEX official daily tick CSV", "available_at": moment, "fetched_at": moment}]
        for key in ("1H", "4H", "1D"):
            fake = FakePool(rows)
            with patch.object(futures_service, "pool", fake):
                result = futures_service.get_futures_intraday("TX", key, 100)
            self.assertEqual(result["count"], 1)
            self.assertEqual(result["latest_session"], "regular")
        app = FastAPI(); app.include_router(router, prefix="/api/v1"); client = TestClient(app)
        with patch("api.routes.futures.get_futures_intraday", return_value={"results": []}):
            self.assertEqual(client.get("/api/v1/futures/intraday?product=TX&range=1H&interval=5m").status_code, 200)
        self.assertEqual(client.get("/api/v1/futures/intraday?product=TE&range=1H").status_code, 422)
        self.assertEqual(client.get("/api/v1/futures/intraday?product=TX&range=30D").status_code, 422)

    def test_removed_old_cards(self):
        html = (ROOT / "futures.html").read_text(encoding="utf-8")
        for heading in ("期現貨價差", "價格 × 未平倉量", "尚未提供"):
            self.assertNotIn(f"<h2>{heading}</h2>", html)


if __name__ == "__main__":
    unittest.main()
