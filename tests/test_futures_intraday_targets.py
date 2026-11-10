from __future__ import annotations

import json
import sys
import unittest
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from research import futures_intraday_targets as target


UTC = timezone.utc
TAIPEI = ZoneInfo("Asia/Taipei")


def stamp(day: date, wall: time) -> datetime:
    return datetime.combine(day, wall, TAIPEI).astimezone(UTC)


def candle(trading_date: date, local_day: date, wall: time, *, contract: str = "TX202609",
           session: str = "after_hours", close: int = 100, open_: int | None = None,
           available: datetime | None = None) -> dict:
    available = available or stamp(trading_date, time(16))
    return {
        "timestamp_utc": stamp(local_day, wall), "trading_date": trading_date, "product": "TX",
        "contract": contract, "session": session, "interval_seconds": 300,
        "open": Decimal(open_ if open_ is not None else close), "high": Decimal(close),
        "low": Decimal(close), "close": Decimal(close), "volume": 1,
        "source": "fixture", "available_at": available, "fetched_at": available,
    }


def fixture_rows(day: date = date(2026, 9, 16)) -> list[dict]:
    prior = date(2026, 9, 15)
    rows = [
        candle(day, prior, time(15), close=100),
        candle(day, prior, time(21, 10), close=101),  # known at 21:15 cutoff
        candle(day, prior, time(21, 25), close=102),  # closes at US DST 09:30
        candle(day, prior, time(22, 25), close=104),  # closes at US DST 10:30
        candle(day, day, time(4, 55), close=105),     # closes at expected night endpoint
        candle(day, day, time(8, 45), session="regular", open_=106, close=107),
        candle(day, day, time(9, 40), session="regular", close=108),
        candle(day, day, time(13, 25), session="regular", close=109),  # expiry close 13:30
    ]
    return rows


class FuturesIntradayTargetsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.calendar = target.load_us_calendar()

    def build(self, rows=None):
        return target.build_intraday_targets(rows or fixture_rows(), self.calendar, {date(2026, 9, 16)})

    def one(self, name, rows=None):
        return next(row for row in self.build(rows) if row["target_name"] == name)

    def test_dst_and_winter_us_open_are_timezone_correct(self):
        summer = target.us_regular_session(date(2026, 9, 15), self.calendar)
        winter = target.us_regular_session(date(2026, 1, 2), self.calendar)
        self.assertEqual(summer.open_at, datetime(2026, 9, 15, 13, 30, tzinfo=UTC))
        self.assertEqual(winter.open_at, datetime(2026, 1, 2, 14, 30, tzinfo=UTC))

    def test_us_holiday_and_uncovered_date_fail_closed(self):
        self.assertIsNone(target.us_regular_session(date(2026, 9, 7), self.calendar))
        self.assertIsNone(target.us_regular_session(date(2027, 1, 4), self.calendar))

    def test_us_targets_cross_midnight_and_use_exact_completed_bars(self):
        one_hour = self.one(target.US_OPEN_TO_1H)
        night = self.one(target.US_OPEN_TO_NIGHT_CLOSE)
        self.assertEqual(one_hour["prediction_at"], datetime(2026, 9, 15, 13, 15, tzinfo=UTC))
        self.assertEqual(one_hour["base_at"], datetime(2026, 9, 15, 13, 30, tzinfo=UTC))
        self.assertEqual(one_hour["endpoint_at"], datetime(2026, 9, 15, 14, 30, tzinfo=UTC))
        self.assertAlmostEqual(one_hour["target_return"], 104 / 102 - 1)
        self.assertEqual(night["endpoint_at"], stamp(date(2026, 9, 16), time(5)))
        self.assertAlmostEqual(night["target_return"], 105 / 102 - 1)

    def test_friday_night_close_uses_saturday_not_monday_trading_label(self):
        day = date(2026, 9, 7)
        rows = [
            candle(day, date(2026, 9, 4), time(15)),
            candle(day, date(2026, 9, 4), time(21, 10)),
            candle(day, date(2026, 9, 4), time(21, 25), close=102),
            candle(day, date(2026, 9, 4), time(22, 25), close=104),
            candle(day, date(2026, 9, 5), time(4, 55), close=105),
            candle(day, day, time(8, 45), session="regular"),
            candle(day, day, time(9, 40), session="regular"),
            candle(day, day, time(13, 40), session="regular"),
        ]
        record = next(row for row in target.build_intraday_targets(rows, self.calendar, {day})
                      if row["target_name"] == target.US_OPEN_TO_NIGHT_CLOSE)
        self.assertEqual(record["endpoint_at"], stamp(date(2026, 9, 5), time(5)))
        self.assertIsNotNone(record["target_return"])

    def test_expiry_1325_bar_is_valid_1330_regular_close(self):
        row = self.one(target.REGULAR_OPEN_TO_CLOSE)
        self.assertEqual(row["endpoint_at"], stamp(date(2026, 9, 16), time(13, 30)))
        self.assertAlmostEqual(row["target_return"], 109 / 106 - 1)

    def test_regular_one_hour_does_not_use_0945_bar_close(self):
        rows = fixture_rows()
        rows.append(candle(date(2026, 9, 16), date(2026, 9, 16), time(9, 45), session="regular", close=999))
        row = self.one(target.REGULAR_OPEN_TO_1H, rows)
        self.assertAlmostEqual(row["target_return"], 108 / 106 - 1)

    def test_missing_expected_endpoint_is_null_not_last_bar(self):
        rows = [row for row in fixture_rows() if row["timestamp_utc"] != stamp(date(2026, 9, 16), time(4, 55))]
        rows.append(candle(date(2026, 9, 16), date(2026, 9, 16), time(4, 50), close=999))
        row = self.one(target.US_OPEN_TO_NIGHT_CLOSE, rows)
        self.assertIsNone(row["target_return"])
        self.assertEqual(row["missing_reason"], "missing_required_price")

    def test_contract_is_fixed_at_prediction_cutoff(self):
        rows = fixture_rows()
        rows.extend([
            candle(date(2026, 9, 16), date(2026, 9, 15), time(21, 25), contract="TX202612", close=10),
            candle(date(2026, 9, 16), date(2026, 9, 15), time(22, 25), contract="TX202612", close=20),
        ])
        row = self.one(target.US_OPEN_TO_1H, rows)
        self.assertEqual(row["contract"], "TX202609")
        self.assertAlmostEqual(row["target_return"], 104 / 102 - 1)

    def test_available_at_is_maximum_of_required_source_prices(self):
        rows = fixture_rows()
        rows[2]["available_at"] = datetime(2026, 9, 16, 8, 0, tzinfo=UTC)
        rows[3]["available_at"] = datetime(2026, 9, 16, 9, 0, tzinfo=UTC)
        row = self.one(target.US_OPEN_TO_1H, rows)
        self.assertEqual(row["target_available_at"], datetime(2026, 9, 16, 9, 0, tzinfo=UTC))

    def test_closed_us_session_preserves_null_targets(self):
        day = date(2026, 9, 8)  # its night begins on NYSE Labor Day
        rows = [candle(day, date(2026, 9, 7), time(15), contract="TX202609"),
                candle(day, day, time(4, 55), contract="TX202609"),
                candle(day, day, time(8, 45), contract="TX202609", session="regular"),
                candle(day, day, time(9, 40), contract="TX202609", session="regular"),
                candle(day, day, time(13, 40), contract="TX202609", session="regular")]
        output = target.build_intraday_targets(rows, self.calendar, {day})
        us = [row for row in output if row["target_name"].startswith("us_")]
        self.assertTrue(all(row["target_return"] is None for row in us))
        self.assertTrue(all(row["missing_reason"] == "unverified_or_closed_us_session" for row in us))
