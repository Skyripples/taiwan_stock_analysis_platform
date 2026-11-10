from __future__ import annotations

import sys
import unittest
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from research import build_futures_day_dataset as day
from research.futures_binary import walk_forward
from research.futures_us_pre_post_targets import HistoricalContractSelection

TAIPEI = ZoneInfo("Asia/Taipei")
UTC = timezone.utc


def feature(day_value: date, *, available: datetime | None = None) -> dict:
    prediction = datetime.combine(day_value, time(8, 30), TAIPEI)
    source = {"prediction_date": day_value, "prediction_at": prediction,
              "tx_available_at": available or prediction - timedelta(hours=12),
              "tx_prior_available_at": available or prediction - timedelta(days=1)}
    for index, name in enumerate(day.FEATURE_COLUMNS):
        source[name] = bool(index % 2) if name in {"tx_expiry_week", "tx_roll_regime"} else float(index + 1)
    return source


def daily(day_value: date, contract: str = "TX202609", open_: float = 100, close: float = 101) -> dict:
    return {"trade_date": day_value, "product": "TX", "session": "regular", "contract": contract,
            "open": open_, "close": close, "available_at": datetime.combine(day_value, time(15, 30), TAIPEI)}


class DayDatasetTests(unittest.TestCase):
    def selection(self, contract="TX202609"):
        return HistoricalContractSelection(contract, date(2026, 9, 16), "unverified_default_rule_availability", {"fixture": True})

    def test_day_uses_0815_and_keeps_flat_out_of_binary(self):
        item = date(2026, 9, 15)
        with patch.object(day, "historical_front_contract", return_value=self.selection()):
            row = day.build_rows([daily(item, close=100)], {item: feature(item)})[0]
        self.assertEqual(row["prediction_at"].astimezone(TAIPEI).time(), time(8, 15))
        self.assertEqual(row["direction_class"], "flat")
        self.assertIsNone(row[day.BINARY_COLUMN])
        self.assertEqual(row["feature_pit_status"], "passed_0815")
        self.assertEqual(row["live_readiness"], "not_assessed_no_0815_live_snapshot")

    def test_revalidation_rejects_source_after_0815_not_just_0830_timestamp(self):
        item = date(2026, 9, 15)
        source = feature(item, available=datetime.combine(item, time(8, 20), TAIPEI))
        _, maximum, reason = day.revalidate_tx_core_at_0815(item, source)
        self.assertIsNone(maximum)
        self.assertTrue(reason.startswith("source_available_after_0815"))

    def test_daily_label_uses_rule_selected_contract_not_volume_or_other_contract(self):
        item = date(2026, 9, 15)
        with patch.object(day, "historical_front_contract", return_value=self.selection("TX202609")):
            row = day.build_rows([daily(item, "TX202610", close=999), daily(item, "TX202609", close=101)], {item: feature(item)})[0]
        self.assertEqual(row["target_contract"], "TX202609")
        self.assertEqual(row["target_close"], 101)

    def test_missing_selected_contract_preserves_null_label(self):
        item = date(2026, 9, 15)
        with patch.object(day, "historical_front_contract", return_value=self.selection("TX202609")):
            row = day.build_rows([daily(item, "TX202610")], {item: feature(item)})[0]
        self.assertIsNone(row[day.TARGET_COLUMN])
        self.assertEqual(row["target_missing_reason"], "missing_selected_contract_regular_daily_row")

    def test_walk_forward_excludes_observed_flat_but_keeps_it_in_test_sessions(self):
        start = date(2024, 1, 2)
        indexed = []
        for index in range(506):
            value = start + timedelta(days=index)
            cutoff = datetime.combine(value, time(8, 15), TAIPEI)
            direction = "flat" if index == 505 else "up"
            indexed.append((index, {"prediction_date": value, "prediction_at": cutoff,
                "target_available_at": cutoff + timedelta(hours=7), "binary_direction_target": None if direction == "flat" else 1,
                "direction_class": direction}))
        folds = walk_forward.expanding_walk_forward_prepared(indexed, allow_resolved_flat=True)
        self.assertEqual(len(folds[0].train_indices), 504)
        self.assertIn(505, folds[-1].test_indices)
        self.assertNotIn(505, folds[-1].train_indices)
