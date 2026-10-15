from __future__ import annotations

import argparse
import sys
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import update_futures_daily as module


class FakeConnection:
    def __init__(self):
        self.commit_count = 0

    def commit(self):
        self.commit_count += 1

    def close(self):
        pass


class FuturesDailyUpdaterTests(unittest.TestCase):
    def test_one_and_multiple_missing_trading_days(self):
        sessions = [date(2026, 10, 14), date(2026, 10, 15)]
        jobs, skipped, _ = module.plan_jobs(list(module.PRODUCTS), sessions, {("TX", sessions[0]), ("MTX", sessions[0])})
        self.assertEqual(len(jobs), 4)
        self.assertEqual(skipped, 2)
        self.assertIn(("TMF", sessions[0]), jobs)

    def test_weekend_holiday_and_incomplete_today_are_not_planned(self):
        now = datetime(2026, 10, 16, 10, 0, tzinfo=module.TAIPEI)
        with patch.object(module, "get_trading_days_in_range", return_value=([date(2026, 10, 15)], [date(2026, 10, 16)])):
            self.assertEqual(module.latest_completed_trading_day(now), date(2026, 10, 15))

    def test_after_cutoff_allows_completed_today(self):
        now = datetime(2026, 10, 16, 16, 0, tzinfo=module.TAIPEI)
        with patch.object(module, "get_trading_days_in_range", return_value=([date(2026, 10, 16)], [])):
            self.assertEqual(module.latest_completed_trading_day(now), date(2026, 10, 16))

    def test_persistent_official_empty_is_skipped(self):
        jobs, _, skipped = module.plan_jobs(list(module.PRODUCTS), [date(2026, 7, 10)], set())
        self.assertEqual(jobs, [])
        self.assertEqual(skipped, 3)

    def test_existing_complete_second_run_has_zero_jobs(self):
        day = date(2026, 10, 15)
        existing = {(product, day) for product in module.PRODUCTS}
        jobs, skipped, _ = module.plan_jobs(list(module.PRODUCTS), [day], existing)
        self.assertEqual(jobs, [])
        self.assertEqual(skipped, 3)

    def test_partial_product_failure_preserves_other_products(self):
        options = argparse.Namespace(products=list(module.PRODUCTS), lookback_days=2, delay=0, dry_run=False)
        day = date(2026, 10, 15)
        latest = {product: date(2026, 10, 14) for product in module.PRODUCTS}
        after = {"TX": day, "MTX": day, "TMF": date(2026, 10, 14)}
        states = [(latest, set()), (latest, set()), (after, set())]
        rows = [{"product": "X"}]

        def fetcher(_controller, _day, product):
            if product == "TMF":
                raise TimeoutError("fixture timeout")
            return rows

        connection = FakeConnection()
        with patch.object(module, "connect", return_value=connection), \
             patch.object(module, "latest_completed_trading_day", return_value=day), \
             patch.object(module, "get_trading_days_in_range", return_value=([day], [])), \
             patch.object(module, "read_db_state", side_effect=states), \
             patch.object(module, "upsert_rows", return_value=1) as upsert:
            result = module.run(options, fetcher=fetcher)
        self.assertEqual(upsert.call_count, 2)
        self.assertEqual(connection.commit_count, 1)
        self.assertEqual(result["downloaded_by_product"], {"TX": 1, "MTX": 1, "TMF": 0})
        self.assertEqual(len(result["failures"]), 1)

    def test_dry_run_performs_zero_network_requests(self):
        options = argparse.Namespace(products=["TX"], lookback_days=2, delay=0, dry_run=True)
        day = date(2026, 10, 15)
        latest = {"TX": date(2026, 10, 14)}
        with patch.object(module, "connect", return_value=FakeConnection()), \
             patch.object(module, "latest_completed_trading_day", return_value=day), \
             patch.object(module, "get_trading_days_in_range", return_value=([day], [])), \
             patch.object(module, "read_db_state", side_effect=[(latest, set()), (latest, set())]):
            result = module.run(options)
        self.assertEqual(result["network_requests"], 0)
        self.assertEqual(result["planned_jobs"], 1)


if __name__ == "__main__":
    unittest.main()
