from __future__ import annotations

import sys
import unittest
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from research.futures_b4_streak import (
    AVAILABLE_COLUMN, BLACK_COLUMN, RED_COLUMN, attach_streak_features,
)


TAIPEI = ZoneInfo("Asia/Taipei")


def source(day: date, *, contract: str = "TX202611", open_: float = 100, close: float = 101,
           available_at: datetime | None = None) -> dict:
    prediction = datetime.combine(day, time(8, 30), TAIPEI)
    return {
        "prediction_date": day, "prediction_at": prediction, "target_contract": contract,
        "target_open": open_, "target_close": close,
        "target_available_at": available_at or prediction + timedelta(hours=7),
    }


class B4StreakTests(unittest.TestCase):
    def test_current_target_is_not_used_and_exact_availability_boundary_is_allowed(self):
        first, second, third = date(2025, 1, 2), date(2025, 1, 3), date(2025, 1, 6)
        first_row = source(first, close=100)  # flat creates an unambiguous reset.
        second_prediction = datetime.combine(second, time(8, 30), TAIPEI)
        first_row["target_available_at"] = second_prediction  # <= cutoff is valid.
        second_row = source(second, close=101)
        third_up = source(third, close=105)
        third_down = source(third, close=95)
        prior = {second: first, third: second}
        result_up, _ = attach_streak_features([first_row, second_row, third_up], previous_session=prior.get)
        result_down, _ = attach_streak_features([first_row, second_row, third_down], previous_session=prior.get)
        up = {row["prediction_date"]: row for row in result_up}[third]
        down = {row["prediction_date"]: row for row in result_down}[third]
        self.assertEqual((up[RED_COLUMN], up[BLACK_COLUMN]), (1, 0))
        self.assertEqual((down[RED_COLUMN], down[BLACK_COLUMN]), (1, 0))
        self.assertEqual(up[AVAILABLE_COLUMN], second_row["target_available_at"])

    def test_flat_roll_and_missing_session_behaviour(self):
        first, second, third, fourth = (date(2025, 2, value) for value in (3, 4, 5, 6))
        rows = [
            source(first, close=100),  # flat
            source(second, close=101),
            source(third, contract="TX202612", close=99),  # roll
            source(fourth, close=101),
        ]
        prior = {second: first, third: second, fourth: date(2025, 2, 5)}
        result, _ = attach_streak_features(rows, previous_session=prior.get)
        by_day = {row["prediction_date"]: row for row in result}
        self.assertEqual((by_day[second][RED_COLUMN], by_day[second][BLACK_COLUMN]), (0, 0))
        self.assertEqual((by_day[third][RED_COLUMN], by_day[third][BLACK_COLUMN]), (0, 0))
        self.assertEqual((by_day[fourth][RED_COLUMN], by_day[fourth][BLACK_COLUMN]), (0, 0))
        missing = attach_streak_features([rows[0], rows[2]], previous_session=prior.get)[0]
        self.assertIsNone({row["prediction_date"]: row for row in missing}[third][RED_COLUMN])

    def test_late_prior_label_produces_null_not_future_leakage(self):
        first, second = date(2025, 3, 3), date(2025, 3, 4)
        second_prediction = datetime.combine(second, time(8, 30), TAIPEI)
        first_row = source(first, close=100, available_at=second_prediction + timedelta(microseconds=1))
        second_row = source(second, close=101)
        result, _ = attach_streak_features([first_row, second_row], previous_session={second: first}.get)
        feature = {row["prediction_date"]: row for row in result}[second]
        self.assertIsNone(feature[RED_COLUMN])
        self.assertIsNone(feature[BLACK_COLUMN])


if __name__ == "__main__":
    unittest.main()
