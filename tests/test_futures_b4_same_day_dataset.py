from __future__ import annotations

import sys
import unittest
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from research import build_futures_b4_same_day_dataset as b4
from research.futures_binary import features, walk_forward


TAIPEI = ZoneInfo("Asia/Taipei")
UTC = timezone.utc


def feature(day: date) -> dict:
    prediction = datetime.combine(day, time(8, 30), TAIPEI)
    row = {"prediction_date": day, "prediction_at": prediction,
           "max_feature_available_at": prediction - timedelta(minutes=1)}
    for index, column in enumerate(b4.FEATURE_COLUMNS):
        row[column] = (index % 2 == 0) if column in {"tx_expiry_week", "tx_roll_regime"} else float(index + 1)
    return row


def daily(day: date, contract: str, open_: float, close: float, *, volume: int = 1, oi: int = 1) -> dict:
    return {"trade_date": day, "contract": contract, "open": open_, "close": close,
            "volume": volume, "open_interest": oi, "settlement_price": 99999.0,
            "available_at": datetime.combine(day, time(15, 30), TAIPEI)}


class B4DatasetTests(unittest.TestCase):
    def test_same_day_target_expiry_close_and_settlement_stay_separate(self):
        day = date(2026, 9, 16)
        rows = b4.build_rows([daily(day, "TX202609", 100, 105)], {day: feature(day)})
        row = rows[0]
        self.assertEqual(row["prediction_date"], day)
        self.assertEqual(row["target_contract"], "TX202609")
        self.assertAlmostEqual(row[b4.TARGET_COLUMN], .05)
        self.assertEqual(row[b4.BINARY_COLUMN], 1)
        self.assertEqual(row["target_close"], 105.0)
        self.assertEqual(row["target_settlement_price"], 99999.0)
        self.assertEqual(row["target_available_at"], datetime(2026, 9, 16, 7, 30, tzinfo=UTC))

    def test_contract_selection_never_uses_final_volume_or_oi(self):
        day = date(2026, 9, 15)
        earliest = daily(day, "TX202609", 100, 101, volume=1, oi=1)
        later = daily(day, "TX202610", 100, 200, volume=999999, oi=999999)
        selected = b4.select_contract([later, earliest], day)
        self.assertEqual(selected["contract"], "TX202609")

    def test_missing_feature_keeps_label_and_marks_reason(self):
        day = date(2026, 9, 15)
        row = b4.build_rows([daily(day, "TX202609", 100, 100)], {})[0]
        self.assertFalse(row["feature_available"])
        self.assertEqual(row["feature_missing_reason"], "missing_pit_feature_row")
        self.assertEqual(row[b4.BINARY_COLUMN], 0)
        self.assertTrue(all(row[column] is None for column in b4.FEATURE_COLUMNS))

    def test_only_explicit_allowlist_can_be_projected_to_x(self):
        day = date(2026, 9, 15)
        row = b4.build_rows([daily(day, "TX202609", 100, 99)], {day: feature(day)})[0]
        x = features.select_feature_rows([row], b4.FEATURE_COLUMNS)[0]
        self.assertEqual(tuple(x), b4.FEATURE_COLUMNS)
        self.assertNotIn("target_close", x)
        self.assertNotIn("dgs10_value", x)

    def test_same_day_label_is_not_trainable_before_1530(self):
        start = date(2024, 1, 2)
        indexed = []
        for offset in range(506):
            day = start + timedelta(days=offset)
            prediction = datetime.combine(day, time(8, 30), TAIPEI)
            indexed.append((offset, {"prediction_date": day, "prediction_at": prediction,
                "target_available_at": prediction + timedelta(hours=7), "binary_direction_target": offset % 2}))
        indexed[505][1]["binary_direction_target"] = None
        indexed[505][1]["target_available_at"] = None
        folds = walk_forward.expanding_walk_forward_prepared(indexed)
        self.assertEqual(folds[0].fit_at, indexed[504][1]["prediction_at"])
        self.assertEqual(len(folds[0].train_indices), 504)
        self.assertIn(505, folds[-1].test_indices)
        self.assertNotIn(505, folds[-1].train_indices)
