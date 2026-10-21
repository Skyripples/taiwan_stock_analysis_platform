from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from research import build_futures_daily_targets as module


UTC = timezone.utc
TAIPEI = ZoneInfo("Asia/Taipei")


def feature_row(day: date, contract: str = "TX202601", *, leaked: bool = False) -> dict:
    prediction_at = datetime.combine(day, datetime.min.time(), TAIPEI).replace(hour=8, minute=30)
    return {
        "prediction_date": day,
        "prediction_at": prediction_at,
        "feature_version": "v4.6-test",
        "tx_contract": contract,
        "max_feature_available_at": prediction_at if not leaked else prediction_at.replace(hour=9),
    }


def tx_row(
    day: date, *, contract: str = "TX202601", open_value: float = 100.0,
    close: float = 101.0, expiry_week: bool = False,
) -> dict:
    return {
        "trade_date": day.isoformat(), "product": "TX", "contract": contract,
        "contract_rank": "front", "session": "regular", "open": open_value,
        "close": close, "is_expiry_week": expiry_week,
        "available_at": f"{day.isoformat()}T15:30:00+08:00",
    }


class FuturesDailyTargetTests(unittest.TestCase):
    def test_next_official_session_skips_weekend(self):
        friday = date(2026, 1, 9)
        monday = date(2026, 1, 12)
        row = module.build_targets([friday, monday], [feature_row(friday)], [tx_row(monday)])[0]
        self.assertEqual(row["target_trading_date"], monday)
        self.assertAlmostEqual(row["next_trading_day_regular_return"], 0.01)
        self.assertEqual(row["direction_class"], "up")

    def test_next_official_session_skips_long_holiday(self):
        before = date(2026, 2, 11)
        after = date(2026, 2, 23)
        row = module.build_targets([before, after], [feature_row(before)], [tx_row(after)])[0]
        self.assertEqual(row["target_trading_date"], after)

    def test_rejects_feature_available_after_prediction(self):
        first = date(2026, 1, 9)
        second = date(2026, 1, 12)
        with self.assertRaisesRegex(ValueError, "Feature leakage"):
            module.build_targets([first, second], [feature_row(first, leaked=True)], [tx_row(second)])

    def test_expiry_and_contract_change_are_flagged(self):
        first = date(2026, 1, 20)
        second = date(2026, 1, 21)
        row = module.build_targets(
            [first, second], [feature_row(first, "TX202601")],
            [tx_row(second, contract="TX202602", expiry_week=True)],
        )[0]
        self.assertTrue(json.loads(row["quality_flags"])["target_roll_regime"])
        self.assertEqual(row["target_contract"], "TX202602")

    def test_missing_target_data_is_null_not_filled(self):
        first = date(2026, 1, 9)
        second = date(2026, 1, 12)
        row = module.build_targets([first, second], [feature_row(first)], [])[0]
        self.assertEqual(row["target_trading_date"], second)
        self.assertIsNone(row["next_trading_day_regular_return"])
        self.assertIsNone(row["direction_class"])
        self.assertTrue(json.loads(row["quality_flags"])["missing_target_session_data"])

    def test_thresholds_are_centralized_and_deterministic(self):
        thresholds = module.DirectionThresholds(-0.01, 0.01)
        self.assertEqual(module.classify_direction(-0.02, thresholds), "down")
        self.assertEqual(module.classify_direction(0.0, thresholds), "neutral")
        self.assertEqual(module.classify_direction(0.02, thresholds), "up")
        first = date(2026, 1, 9)
        second = date(2026, 1, 12)
        built_once = module.build_targets([first, second], [feature_row(first)], [tx_row(second)])
        built_twice = module.build_targets([first, second], [feature_row(first)], [tx_row(second)])
        self.assertEqual(built_once, built_twice)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            module.write_rows(built_once, root)
            module.write_rows(built_twice, root)
            import pyarrow.parquet as pq
            stored = pq.ParquetFile(root / "partition_year=2026/part-00000.parquet").read().to_pylist()
            self.assertEqual(len(stored), 1)


if __name__ == "__main__":
    unittest.main()
