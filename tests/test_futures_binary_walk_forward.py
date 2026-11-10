from __future__ import annotations

import sys
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from research.futures_binary import walk_forward


TAIPEI = ZoneInfo("Asia/Taipei")


def row(day: date, value: float | None = 0.01, available_at: datetime | None = None) -> dict:
    prediction_at = datetime.combine(day, datetime.min.time(), TAIPEI).replace(hour=8, minute=30)
    target_day = day + timedelta(days=1) if value is not None else None
    return {
        "prediction_date": day,
        "prediction_at": prediction_at,
        "tx_available_at": prediction_at - timedelta(hours=12),
        "max_feature_available_at": prediction_at - timedelta(hours=1),
        "target_trading_date": target_day,
        "target_available_at": available_at if value is not None else None,
        "next_trading_day_regular_return": value,
    }


def series(count: int) -> list[dict]:
    start = date(2023, 1, 1)
    rows = []
    for offset in range(count):
        day = start + timedelta(days=offset)
        prediction = datetime.combine(day, datetime.min.time(), TAIPEI).replace(hour=8, minute=30)
        rows.append(row(day, 0.01 if offset % 2 else -0.01, prediction + timedelta(hours=16)))
    return rows


class WalkForwardTests(unittest.TestCase):
    def test_expanding_window_keeps_last_partial_test_fold(self):
        rows = series(550)
        folds = walk_forward.expanding_walk_forward(rows)
        self.assertEqual([len(fold.test_indices) for fold in folds], [21, 21, 4])
        self.assertEqual(folds[0].fit_at, rows[504]["prediction_at"])
        self.assertEqual(len(folds[0].train_indices), 504)
        self.assertEqual(folds[-1].test_indices, (546, 547, 548, 549))

    def test_test_sessions_do_not_overlap_and_missing_target_stays_in_test(self):
        rows = series(550)
        rows[548] = row(rows[548]["prediction_date"], None)
        folds = walk_forward.expanding_walk_forward(rows)
        test_indices = [index for fold in folds for index in fold.test_indices]
        self.assertEqual(len(test_indices), len(set(test_indices)))
        self.assertIn(548, folds[-1].test_indices)

    def test_training_uses_only_labels_available_at_fit_time(self):
        rows = series(507)
        rows[502]["target_available_at"] = rows[504]["prediction_at"] + timedelta(microseconds=1)
        folds = walk_forward.expanding_walk_forward(rows, initial_train_size=503, test_size=2)
        first = folds[0]
        self.assertEqual(first.fit_at, rows[504]["prediction_at"])
        self.assertNotIn(502, first.train_indices)
        self.assertIn(502, first.purged_indices)
        for index in first.train_indices:
            self.assertLess(rows[index]["prediction_at"], first.fit_at)
            self.assertLessEqual(rows[index]["target_available_at"], first.fit_at)

    def test_indices_preserve_original_input_order(self):
        rows = series(7)
        reordered = [rows[2], rows[0], rows[1], *rows[3:]]
        folds = walk_forward.expanding_walk_forward(reordered, initial_train_size=2, test_size=2)
        self.assertEqual(folds[0].test_indices, (0, 3))
        self.assertEqual(folds[0].fit_at, rows[2]["prediction_at"])

    def test_cross_year_and_late_labels_are_purged_not_shuffled(self):
        rows = []
        for offset in range(6):
            day = date(2025, 12, 30) + timedelta(days=offset)
            prediction = datetime.combine(day, datetime.min.time(), TAIPEI).replace(hour=8, minute=30)
            rows.append(row(day, 0.01, prediction + timedelta(hours=16)))
        rows[0]["target_available_at"] = rows[2]["prediction_at"] + timedelta(hours=1)
        folds = walk_forward.expanding_walk_forward(rows, initial_train_size=1, test_size=2)
        self.assertTrue(folds)
        self.assertIn(0, folds[0].purged_indices)
        self.assertFalse(set(folds[0].train_indices) & set(folds[0].test_indices))

    def test_insufficient_revealed_history_and_invalid_sizes(self):
        self.assertEqual(walk_forward.expanding_walk_forward(series(10)), ())
        for value in (0, -1, True, 1.2):
            with self.assertRaises(ValueError):
                walk_forward.expanding_walk_forward(series(3), initial_train_size=value)
            with self.assertRaises(ValueError):
                walk_forward.expanding_walk_forward(series(3), test_size=value)


if __name__ == "__main__":
    unittest.main()
