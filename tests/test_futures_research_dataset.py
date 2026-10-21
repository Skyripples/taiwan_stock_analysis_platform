from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from research import build_futures_research_dataset as module


TAIPEI = ZoneInfo("Asia/Taipei")


def feature(day: date, value: float = 1.0) -> dict:
    prediction_at = datetime.combine(day, datetime.min.time(), TAIPEI).replace(hour=8, minute=30)
    return {
        "partition_year": day.year,
        "prediction_date": day,
        "prediction_at": prediction_at,
        "feature_version": "v4.6-test",
        "tx_close": value,
        "quality_flags": '{"missing_pcr_oi":true}',
    }


def target(day: date, *, value: float | None = 0.01, direction: str | None = "up") -> dict:
    return {
        "prediction_date": day,
        "prediction_at": feature(day)["prediction_at"],
        "target_version": "v4.6-test",
        "target_trading_date": date(2026, 1, 12),
        "target_contract": "TX202601",
        "target_available_at": None,
        "next_trading_day_regular_return": value,
        "direction_class": direction,
        "direction_down_max": -0.002,
        "direction_up_min": 0.002,
        "quality_flags": '{"missing_target_session_data":false}',
    }


class FuturesResearchDatasetTests(unittest.TestCase):
    def test_left_join_preserves_features_and_correct_target(self):
        first = date(2026, 1, 9)
        second = date(2026, 1, 12)
        rows = module.build_dataset([feature(first, 11), feature(second, 22)], [target(first)])
        self.assertEqual([row["prediction_date"] for row in rows], [first, second])
        self.assertEqual(rows[0]["tx_close"], 11)
        self.assertAlmostEqual(rows[0]["next_trading_day_regular_return"], 0.01)
        self.assertEqual(rows[0]["quality_flags"], '{"missing_pcr_oi":true}')
        self.assertIsNone(rows[1]["next_trading_day_regular_return"])
        self.assertIsNone(rows[1]["direction_class"])

    def test_missing_target_values_are_retained_without_imputation(self):
        day = date(2026, 1, 9)
        row = module.build_dataset([feature(day)], [target(day, value=None, direction=None)])[0]
        self.assertIsNone(row["next_trading_day_regular_return"])
        self.assertIsNone(row["direction_class"])
        self.assertIsNotNone(row["target_quality_flags"])

    def test_duplicate_prediction_dates_are_rejected(self):
        day = date(2026, 1, 9)
        with self.assertRaisesRegex(ValueError, "Duplicate feature"):
            module.build_dataset([feature(day), feature(day)], [])
        with self.assertRaisesRegex(ValueError, "Duplicate target"):
            module.build_dataset([feature(day)], [target(day), target(day)])

    def test_write_is_idempotent_by_prediction_date(self):
        day = date(2026, 1, 9)
        rows = module.build_dataset([feature(day)], [target(day)])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            module.write_rows(rows, root)
            module.write_rows(rows, root)
            stored = module.parquet_rows(root / "partition_year=2026" / "part-00000.parquet")
            self.assertEqual(len(stored), 1)
            self.assertEqual(stored[0]["prediction_date"], day)


if __name__ == "__main__":
    unittest.main()
