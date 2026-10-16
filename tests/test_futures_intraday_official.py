from __future__ import annotations

import argparse
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import backfill_futures_intraday_5m as backfill
from futures_intraday import FuturesTick
from futures_intraday_reconstruction import aggregate_ticks, session_for_file_tick


def item(stamp, product="TX", contract="TX202610", price="100", volume=1):
    from decimal import Decimal
    return FuturesTick(datetime.fromisoformat(stamp), product, contract, Decimal(price), volume)


class OfficialReconstructionTests(unittest.TestCase):
    def test_same_time_order_volume_and_gap_preservation(self):
        ticks = [item("2026-10-16T08:45:01+08:00", price="100", volume=2),
                 item("2026-10-16T08:45:01+08:00", price="102", volume=3),
                 item("2026-10-16T08:55:01+08:00", price="101", volume=4)]
        candles = aggregate_ticks(ticks, date(2026, 10, 16), available_at=datetime.now(timezone.utc))
        self.assertEqual(len(candles), 2)
        self.assertEqual((candles[0].open, candles[0].close, candles[0].volume),
                         (ticks[0].price, ticks[1].price, 5))
        self.assertEqual((candles[1].timestamp_utc - candles[0].timestamp_utc).total_seconds(), 600)

    def test_regular_after_hours_and_cross_midnight_file_date(self):
        day = date(2026, 10, 16)
        self.assertEqual(session_for_file_tick(item("2026-10-15T15:01:00+08:00"), day), "after_hours")
        self.assertEqual(session_for_file_tick(item("2026-10-16T02:00:00+08:00"), day), "after_hours")
        self.assertEqual(session_for_file_tick(item("2026-10-16T08:45:00+08:00"), day), "regular")
        with self.assertRaises(ValueError): session_for_file_tick(item("2026-10-16T14:00:00+08:00"), day)

    def test_monday_uses_previous_friday_night_window(self):
        day = date(2026, 9, 7)
        self.assertEqual(session_for_file_tick(item("2026-09-04T15:00:00+08:00"), day), "after_hours")
        self.assertEqual(session_for_file_tick(item("2026-09-05T05:00:00+08:00"), day), "after_hours")
        with self.assertRaises(ValueError):
            session_for_file_tick(item("2026-09-07T02:00:00+08:00"), day)

    def test_normal_tuesday_and_regular_session(self):
        day = date(2026, 9, 8)
        self.assertEqual(session_for_file_tick(item("2026-09-07T15:00:00+08:00"), day), "after_hours")
        self.assertEqual(session_for_file_tick(item("2026-09-08T08:45:00+08:00"), day), "regular")
        self.assertEqual(session_for_file_tick(item("2026-09-08T13:45:59+08:00"), day), "regular")

    def test_long_weekend_first_session_uses_last_official_session(self):
        day = date(2026, 9, 29)
        self.assertEqual(session_for_file_tick(item("2026-09-24T15:00:00+08:00"), day), "after_hours")
        self.assertEqual(session_for_file_tick(item("2026-09-25T05:00:00+08:00"), day), "after_hours")
        with self.assertRaises(ValueError):
            session_for_file_tick(item("2026-09-28T15:00:00+08:00"), day)

    def test_six_failed_dates_resolve_their_previous_session(self):
        expected = {
            date(2026, 9, 7): date(2026, 9, 4), date(2026, 9, 14): date(2026, 9, 11),
            date(2026, 9, 21): date(2026, 9, 18), date(2026, 9, 29): date(2026, 9, 24),
            date(2026, 10, 5): date(2026, 10, 2), date(2026, 10, 12): date(2026, 10, 8),
        }
        for trading_day, prior in expected.items():
            with self.subTest(trading_day=trading_day):
                self.assertEqual(session_for_file_tick(item(f"{prior.isoformat()}T15:00:00+08:00"), trading_day), "after_hours")
                self.assertEqual(session_for_file_tick(item(f"{(prior + timedelta(days=1)).isoformat()}T05:00:00+08:00"), trading_day), "after_hours")

    def test_actual_contract_and_three_products_preserved(self):
        ticks = [item("2026-10-16T08:45:00+08:00", p, p + "202611") for p in ("TX", "MTX", "TMF")]
        candles = aggregate_ticks(ticks, date(2026, 10, 16), available_at=datetime.now(timezone.utc))
        self.assertEqual({(row.product, row.contract) for row in candles},
                         {(p, p + "202611") for p in ("TX", "MTX", "TMF")})

    def test_dry_run_is_zero_network(self):
        options = argparse.Namespace(start="2026-10-16", end="2026-10-16", products=["TX", "MTX", "TMF"],
                                     resume=True, dry_run=True, apply=False, delay=0)
        with patch.object(backfill, "get_trading_days_in_range", return_value=([date(2026, 10, 16)], [])), \
             patch.object(backfill, "download_with_retry") as network:
            result = backfill.run(options)
        network.assert_not_called(); self.assertEqual(result["network_requests"], 0)

    def test_resume_skips_completed_product_partition(self):
        options = argparse.Namespace(start="2026-10-16", end="2026-10-16", products=["TX"],
                                     resume=True, dry_run=True, apply=False, delay=0)
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(backfill, "STAGING", Path(directory)), \
             patch.object(backfill, "CHECKPOINT", Path(directory) / "checkpoint.json"), \
             patch.object(backfill, "get_trading_days_in_range", return_value=([date(2026, 10, 16)], [])), \
             patch.object(backfill, "load_checkpoint", return_value={"completed": {"2026-10-16": ["TX"]}}), \
             patch.object(backfill, "completed", return_value=True):
            result = backfill.run(options)
        self.assertEqual(result["planned_files"], 0); self.assertEqual(result["skipped_existing"], 1)

    def test_service_is_daily_not_streaming(self):
        service = (ROOT / "deploy/futures-intraday-5m-update.service").read_text(encoding="utf-8")
        timer = (ROOT / "deploy/futures-intraday-5m-update.timer").read_text(encoding="utf-8")
        self.assertIn("Type=oneshot", service); self.assertIn("16:30:00 Asia/Taipei", timer)
        self.assertNotIn("Shioaji", service + timer)

    def test_partial_morning_file_is_not_completed(self):
        source = (ROOT / "scripts/backfill_futures_intraday_5m.py").read_text(encoding="utf-8")
        self.assertIn('candle.session == "regular"', source)
        self.assertIn('status": "not-yet-published"', source)


if __name__ == "__main__": unittest.main()
