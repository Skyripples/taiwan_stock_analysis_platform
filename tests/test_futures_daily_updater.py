from __future__ import annotations

import argparse
import sys
import unittest
from datetime import date, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
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
    @staticmethod
    def staging_row(day: str, contract: str, rank: str = "front", close: float = 100.0) -> dict:
        return {
            "trade_date": day, "product": "TX", "contract": contract,
            "contract_month": contract.removeprefix("TX"), "session": "regular",
            "open": close - 1, "high": close + 1, "low": close - 2, "close": close,
            "settlement_price": close, "volume": 100, "open_interest": 200,
            "expiry_date": "2026-11-18", "days_to_expiry": 20,
            "roll_date": "2026-11-11", "is_expiry_week": False,
            "contract_rank": rank, "spot_index": None, "basis": None, "basis_pct": None,
            "source": "https://example.test/taifex", "source_date": day,
            "available_at": f"{day}T15:30:00+08:00",
            "fetched_at": "2026-10-23T08:00:00+00:00",
        }

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

    def test_tx_refreshes_small_lookback_even_when_db_is_complete(self):
        day = date(2026, 10, 15)
        existing = {(product, day) for product in module.PRODUCTS}
        jobs, skipped, _ = module.plan_jobs(
            list(module.PRODUCTS), [day], existing, refresh_products={"TX"},
        )
        self.assertEqual(jobs, [("TX", day)])
        self.assertEqual(skipped, 2)

    def test_tx_staging_append_is_idempotent_and_preserves_history(self):
        first = self.staging_row("2026-10-20", "TX202610", close=100.0)
        second = self.staging_row("2026-10-21", "TX202610", close=101.0)
        replacement = self.staging_row("2026-10-21", "TX202610", close=102.0)
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            module.sync_tx_staging([first], staging_root=root)
            module.sync_tx_staging([second], staging_root=root)
            module.sync_tx_staging([replacement], staging_root=root)
            path = root / "product=TX" / "year=2026" / "daily.jsonl.gz"
            rows = module.read_staged_rows(path)
        self.assertEqual([row["trade_date"] for row in rows], ["2026-10-20", "2026-10-21"])
        self.assertEqual(rows[-1]["close"], 102.0)

    def test_empty_tx_sync_does_not_create_a_non_trading_day_file(self):
        with TemporaryDirectory() as temporary:
            result = module.sync_tx_staging([], staging_root=Path(temporary))
            self.assertEqual(list(Path(temporary).rglob("*.jsonl.gz")), [])
        self.assertEqual(result, {"rows": 0, "updated_dates": [], "partitions": 0})

    def test_staging_rows_are_compatible_with_the_pit_reader(self):
        from research.build_futures_daily_pit_alignment import read_tx_daily

        front = self.staging_row("2026-10-21", "TX202610", "front")
        next_contract = self.staging_row("2026-10-21", "TX202611", "next")
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            module.sync_tx_staging([front, next_contract], staging_root=root)
            rows = read_tx_daily(root=root / "product=TX")
        self.assertEqual(len(rows), 2)
        self.assertEqual({row["contract_rank"] for row in rows}, {"front", "next"})
        self.assertTrue(all(row["available_at"].endswith("+08:00") for row in rows))

    def test_staging_only_uses_bounded_tx_jobs_without_a_database_connection(self):
        options = argparse.Namespace(lookback_days=2, delay=0, dry_run=False)
        day = date(2026, 10, 15)
        rows = [self.staging_row(day.isoformat(), "TX202610")]
        with TemporaryDirectory() as temporary, \
             patch.object(module, "FUTURES_DAILY_STAGING", Path(temporary)), \
             patch.object(module, "latest_completed_trading_day", return_value=day), \
             patch.object(module, "get_trading_days_in_range", return_value=([day], [])), \
             patch.object(module, "sync_tx_staging", return_value={"rows": 1, "updated_dates": [day.isoformat()], "partitions": 1}) as sync:
            result = module.run_staging_only(options, fetcher=lambda _controller, _day, _product: rows)
        self.assertEqual(result["staging_rows"], 1)
        self.assertEqual(result["staging_updated_dates"], [day.isoformat()])
        sync.assert_called_once_with(rows)

    def test_trailing_official_empty_is_not_yet_published_skip_without_fake_row(self):
        prior, latest = date(2026, 10, 22), date(2026, 10, 23)
        options = argparse.Namespace(lookback_days=2, delay=0, dry_run=False)
        prior_rows = [self.staging_row(prior.isoformat(), "TX202610")]

        def fetcher(_controller, day, _product):
            if day == latest:
                raise module.OfficialEmpty("not published")
            return prior_rows

        with patch.object(module, "latest_completed_trading_day", return_value=latest), \
             patch.object(module, "get_trading_days_in_range", return_value=([prior, latest], [])), \
             patch.object(module, "tx_staging_dates", return_value={prior}), \
             patch.object(module, "sync_tx_staging", return_value={"rows": 1, "updated_dates": [prior.isoformat()], "partitions": 1}) as sync:
            result = module.run_staging_only(options, fetcher=fetcher)
        self.assertEqual(result["failures"], [])
        self.assertEqual(result["skipped_not_yet_published"], [{"product": "TX", "date": latest.isoformat(), "reason": "not_yet_published"}])
        self.assertEqual(result["staging_updated_dates"], [prior.isoformat()])
        self.assertEqual(sync.call_count, 1)

    def test_historical_or_internal_official_empty_remains_failure(self):
        missing, latest = date(2026, 10, 22), date(2026, 10, 23)
        options = argparse.Namespace(lookback_days=2, delay=0, dry_run=False)
        def missing_fetcher(_controller, day, _product):
            if day == missing:
                raise module.OfficialEmpty("gap")
            raise module.OfficialEmpty("trailing")
        with patch.object(module, "latest_completed_trading_day", return_value=latest), \
             patch.object(module, "get_trading_days_in_range", return_value=([missing, latest], [])), \
             patch.object(module, "tx_staging_dates", return_value=set()):
            result = module.run_staging_only(options, fetcher=missing_fetcher)
        self.assertEqual(len(result["failures"]), 1)
        self.assertEqual(result["failures"][0]["date"], missing.isoformat())
        self.assertEqual(result["skipped_not_yet_published"][0]["date"], latest.isoformat())

    def test_trailing_parser_or_network_error_remains_failure(self):
        latest = date(2026, 10, 23)
        options = argparse.Namespace(lookback_days=1, delay=0, dry_run=False)
        with patch.object(module, "latest_completed_trading_day", return_value=latest), \
             patch.object(module, "get_trading_days_in_range", return_value=([latest], [])), \
             patch.object(module, "tx_staging_dates", return_value=set()):
            result = module.run_staging_only(options, fetcher=lambda *_args: (_ for _ in ()).throw(ValueError("parser failure")))
        self.assertEqual(len(result["failures"]), 1)
        self.assertEqual(result["skipped_not_yet_published"], [])

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
