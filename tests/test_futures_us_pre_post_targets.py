from __future__ import annotations

import sys
import unittest
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from research import futures_us_pre_post_targets as target


UTC = timezone.utc
TAIPEI = ZoneInfo("Asia/Taipei")


def stamp(day: date, wall: time) -> datetime:
    return datetime.combine(day, wall, TAIPEI).astimezone(UTC)


def candle(trading_day: date, local_day: date, wall: time, *, contract: str = "TX202609",
           close: int = 100, available: datetime | None = None, fetched: datetime | None = None) -> dict:
    completed = stamp(local_day, wall) + timedelta(minutes=5)
    return {
        "timestamp_utc": stamp(local_day, wall), "trading_date": trading_day,
        "product": "TX", "contract": contract, "session": "after_hours", "interval_seconds": 300,
        "open": Decimal(close), "high": Decimal(close), "low": Decimal(close), "close": Decimal(close),
        "volume": 1, "source": "fixture", "available_at": available or completed,
        "fetched_at": fetched or completed,
    }


def rows_for_dst_day(day: date = date(2026, 9, 16), evening: date = date(2026, 9, 15)) -> list[dict]:
    # NY DST: 09:00/10:00/11:00 = Taipei 21:00/22:00/23:00.
    return [
        candle(day, evening, time(20, 50), close=99),
        candle(day, evening, time(20, 55), close=100),  # US_PRE start close
        candle(day, evening, time(21, 55), close=110),  # US_PRE end / US_POST start
        candle(day, evening, time(22, 55), close=105),  # US_POST end
    ]


class UsPrePostTargetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.calendar = target.load_us_calendar()

    def build(self, rows=None):
        with patch.object(target, "taifex_session_arrangements", return_value={"after_hours": {"state": "expected"}}):
            return target.build_us_pre_post_targets(rows or rows_for_dst_day(), self.calendar)

    def one(self, name, rows=None):
        return next(row for row in self.build(rows) if row["target_name"] == name)

    def test_dst_windows_and_exact_completed_endpoint_bars(self):
        pre = self.one(target.US_PRE)
        post = self.one(target.US_POST)
        self.assertEqual(pre["prediction_at"], datetime(2026, 9, 15, 13, 0, tzinfo=UTC))
        self.assertEqual(pre["start_at"], datetime(2026, 9, 15, 13, 0, tzinfo=UTC))
        self.assertEqual(pre["end_at"], datetime(2026, 9, 15, 14, 0, tzinfo=UTC))
        self.assertAlmostEqual(pre["target_return"], 0.1)
        self.assertAlmostEqual(post["target_return"], 105 / 110 - 1)
        self.assertEqual(pre["direction_class"], "UP")
        self.assertEqual(post["direction_class"], "DOWN")

    def test_winter_dst_conversion(self):
        windows = target.us_target_windows(date(2026, 1, 2), self.calendar)
        self.assertEqual(windows[0].start_at, datetime(2026, 1, 2, 14, 0, tzinfo=UTC))
        self.assertEqual(windows[1].end_at, datetime(2026, 1, 2, 16, 0, tzinfo=UTC))

    def test_us_holiday_is_null_not_a_weekday_assumption(self):
        rows = rows_for_dst_day(date(2026, 9, 8), date(2026, 9, 7))  # Labor Day.
        output = self.build(rows)
        self.assertTrue(all(row["target_return"] is None for row in output))
        self.assertTrue(all(row["missing_reason"] == "unverified_or_closed_us_session" for row in output))

    def test_missing_exact_endpoint_never_uses_last_bar(self):
        rows = rows_for_dst_day()
        rows = [row for row in rows if row["timestamp_utc"] != stamp(date(2026, 9, 15), time(21, 55))]
        rows.append(candle(date(2026, 9, 16), date(2026, 9, 15), time(21, 50), close=999))
        row = self.one(target.US_PRE, rows)
        self.assertIsNone(row["target_return"])
        self.assertEqual(row["missing_reason"], "missing_required_endpoint")

    def test_historical_selection_uses_rule_even_when_reconstructed_file_was_fetched_late(self):
        rows = rows_for_dst_day()
        for row in rows:
            row["available_at"] = datetime(2026, 9, 16, 8, 0, tzinfo=UTC)
            row["fetched_at"] = datetime(2026, 9, 16, 8, 0, tzinfo=UTC)
        pre = self.one(target.US_PRE, rows)
        self.assertEqual(pre["contract"], "TX202609")
        self.assertIsNotNone(pre["target_return"])
        self.assertEqual(pre["contract_selection_evidence_status"], "unverified_default_rule_availability")
        self.assertEqual(pre["live_contract_evidence_status"], "failed_no_cutoff_acquisition_evidence")
        self.assertEqual(pre["live_readiness"], "not_ready_historical_reconstruction_only")

    def test_cutoff_selection_never_uses_later_contract_bar_or_final_volume(self):
        rows = rows_for_dst_day()
        rows.extend([
            candle(date(2026, 9, 16), date(2026, 9, 15), time(21, 5), contract="TX202612", close=1),
            candle(date(2026, 9, 16), date(2026, 9, 15), time(21, 55), contract="TX202612", close=2),
        ])
        pre = self.one(target.US_PRE, rows)
        self.assertEqual(pre["contract"], "TX202609")

    def test_historical_contract_rule_fails_closed_without_calendar_evidence(self):
        with patch.object(target, "is_taifex_trading_day", return_value=False):
            selection = target.historical_front_contract(datetime(2026, 9, 15, 13, tzinfo=UTC))
        self.assertIsNone(selection.contract)
        self.assertEqual(selection.evidence_status, "unavailable_contract_rule")

    def test_202602_official_exception_delays_expiry_to_february_23(self):
        selection = target.historical_front_contract(datetime(2026, 2, 18, 4, tzinfo=UTC))
        self.assertEqual(selection.contract, "TX202602")
        self.assertEqual(selection.expiry_date, date(2026, 2, 23))
        self.assertEqual(selection.evidence_status, "verified_official_exception")
        self.assertEqual(selection.basis["source_name"].split(",")[0], "TAIFEX Lunar New Year 2026 market-closure announcement")

    def test_expiring_contract_is_not_selected_after_actual_expiry_time(self):
        before_close = target.historical_front_contract(datetime(2026, 9, 16, 5, 29, tzinfo=UTC))
        after_close = target.historical_front_contract(datetime(2026, 9, 16, 5, 31, tzinfo=UTC))
        self.assertEqual(before_close.contract, "TX202609")
        self.assertEqual(after_close.contract, "TX202610")

    def test_flat_is_distinct_from_down(self):
        rows = rows_for_dst_day()
        for row in rows:
            if row["timestamp_utc"] == stamp(date(2026, 9, 15), time(21, 55)):
                row["close"] = Decimal(100)
        row = self.one(target.US_PRE, rows)
        self.assertEqual(row["target_return"], 0.0)
        self.assertEqual(row["direction_class"], "FLAT")

    def test_target_available_at_is_the_later_required_endpoint(self):
        rows = rows_for_dst_day()
        for row in rows:
            if row["timestamp_utc"] == stamp(date(2026, 9, 15), time(20, 55)):
                row["available_at"] = datetime(2026, 9, 15, 13, 0, tzinfo=UTC)
            if row["timestamp_utc"] == stamp(date(2026, 9, 15), time(21, 55)):
                row["available_at"] = datetime(2026, 9, 15, 14, 5, tzinfo=UTC)
        row = self.one(target.US_PRE, rows)
        self.assertEqual(row["target_available_at"], datetime(2026, 9, 15, 14, 5, tzinfo=UTC))

    def test_cross_holiday_after_hours_keeps_the_actual_evening_us_date(self):
        # The 2026-10-22 evening is attributed to the next TAIFEX trading day, 10/27.
        rows = rows_for_dst_day(date(2026, 10, 27), date(2026, 10, 22))
        # October is DST, so move the fixture bars from 20:xx to the same wall times.
        output = self.build(rows)
        pre = next(row for row in output if row["target_name"] == target.US_PRE)
        self.assertEqual(pre["prediction_at"], datetime(2026, 10, 22, 13, 0, tzinfo=UTC))
        self.assertEqual(pre["trading_date"], date(2026, 10, 27))

    def test_taifex_not_scheduled_does_not_claim_missing_market_data(self):
        with patch.object(target, "taifex_session_arrangements", return_value={"after_hours": {"state": "not_scheduled"}}):
            output = target.build_us_pre_post_targets(rows_for_dst_day(), self.calendar)
        self.assertTrue(all(row["missing_reason"] == "taifex_after_hours_not_scheduled" for row in output))
