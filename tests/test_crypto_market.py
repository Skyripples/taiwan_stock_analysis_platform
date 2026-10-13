from __future__ import annotations

import argparse
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import backfill_crypto_history
from crypto_market import (completed_candle_end, completed_rows, derived_metrics, missing_candle_windows,
                           normalize_rows, read_parquet, utc_datetime, validate_candle,
                           write_parquet_atomic)
from research.backfill_global_market import fetch_crypto_chunk

UTC = timezone.utc


def candle(symbol: str, moment: datetime, close: float = 101.0) -> dict:
    return {"symbol": symbol, "timestamp_utc": moment.isoformat(), "interval_seconds": 300,
            "open": 100.0, "high": 102.0, "low": 99.0, "close": close, "volume": 5.0,
            "timezone": "UTC", "available_at": (moment + timedelta(minutes=5)).isoformat(),
            "quality_flags": {}, "metadata": {"product": symbol}, "source": "Coinbase Exchange",
            "fetched_at": datetime.now(UTC).isoformat()}


class CryptoMarketTests(unittest.TestCase):
    def test_coinbase_five_minute_parsing_for_btc_and_eth(self):
        start = datetime(2026, 1, 1, tzinfo=UTC)
        for product in ("BTC-USD", "ETH-USD"):
            response = Mock()
            response.json.return_value = [[int(start.timestamp()), 99, 102, 100, 101, 5]]
            client = Mock(); client.get.return_value = response
            rows = fetch_crypto_chunk(client, product, start, start + timedelta(minutes=5), 300)
            self.assertEqual(rows[0]["interval_seconds"], 300)
            self.assertEqual(rows[0]["available_at"], (start + timedelta(minutes=5)).isoformat())

    def test_ohlcv_and_utc_validation(self):
        valid = candle("BTC-USD", datetime(2026, 1, 1, tzinfo=UTC))
        self.assertEqual(utc_datetime(validate_candle(valid)["timestamp_utc"]).utcoffset(), timedelta(0))
        with self.assertRaises(ValueError):
            validate_candle({**valid, "high": 98.0})
        with self.assertRaises(ValueError):
            validate_candle({**valid, "timestamp_utc": "2026-01-01T00:00:00"})

    def test_dedupe_and_atomic_parquet(self):
        moment = datetime(2026, 1, 1, tzinfo=UTC)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "candles.parquet"
            write_parquet_atomic(path, [candle("BTC-USD", moment, 100), candle("BTC-USD", moment, 101)])
            rows = read_parquet(path)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["close"], 101.0)

    def test_gap_detection_and_completed_boundary(self):
        start = datetime(2026, 1, 1, tzinfo=UTC)
        existing = {start, start + timedelta(minutes=10)}
        self.assertEqual(missing_candle_windows(start, start + timedelta(minutes=15), existing), [(start + timedelta(minutes=5), start + timedelta(minutes=10))])
        self.assertEqual(completed_candle_end(datetime(2026, 1, 1, 12, 3, tzinfo=UTC)), datetime(2026, 1, 1, 12, 0, tzinfo=UTC))
        boundary = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
        rows = [candle("BTC-USD", boundary - timedelta(minutes=5)), candle("BTC-USD", boundary)]
        self.assertEqual([row["timestamp_utc"] for row in completed_rows(rows, boundary)], [(boundary - timedelta(minutes=5)).isoformat()])

    def test_resume_does_not_refetch_complete_staging(self):
        start = datetime(2026, 1, 1, tzinfo=UTC); end = start + timedelta(minutes=10)
        options = argparse.Namespace(symbols=["BTC-USD"], years=1, start=start, end=end, resume=True, dry_run=False)
        rows = [candle("BTC-USD", start), candle("BTC-USD", start + timedelta(minutes=5))]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(backfill_crypto_history, "ROOT", root), patch.object(backfill_crypto_history, "CHECKPOINT", root / "checkpoint.json"), patch.object(backfill_crypto_history, "SUMMARY", root / "summary.json"), patch.object(backfill_crypto_history, "fetch_crypto_chunk", return_value=rows) as fetch:
                first = backfill_crypto_history.run(options, client=Mock())
                second = backfill_crypto_history.run(options, client=Mock())
            self.assertEqual(first["downloaded_rows"], 2)
            self.assertEqual(second["downloaded_rows"], 0)
            self.assertEqual(fetch.call_count, 1)

    def test_dry_run_uses_zero_network_and_short_metrics_are_null(self):
        end = completed_candle_end(datetime.now(UTC)); start = end - timedelta(minutes=10)
        options = argparse.Namespace(symbols=["ETH-USD"], years=1, start=start, end=end, resume=False, dry_run=True)
        with tempfile.TemporaryDirectory() as directory, patch.object(backfill_crypto_history, "ROOT", Path(directory)), patch.object(backfill_crypto_history, "Client") as client:
            result = backfill_crypto_history.run(options)
        self.assertEqual(result["network_requests"], 0)
        client.assert_not_called()
        metrics = derived_metrics([candle("ETH-USD", start)])
        self.assertIsNone(metrics["return_1h"])
        self.assertIsNone(metrics["ma20"])


if __name__ == "__main__":
    unittest.main()
