from __future__ import annotations

import sys
import csv
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from unittest.mock import patch

from update_rule_forecast import dynamic_weights, ensure_forecast_timeliness, update_history


class RuleForecastProductionTests(unittest.TestCase):
    def test_target_cutoff_rejects_late_generation(self):
        taipei = ZoneInfo("Asia/Taipei")
        ensure_forecast_timeliness("2026-09-07", datetime(2026, 9, 7, 8, 59, tzinfo=taipei))
        with self.assertRaisesRegex(ValueError, "cutoff has passed"):
            ensure_forecast_timeliness("2026-09-07", datetime(2026, 9, 7, 9, 0, tzinfo=taipei))

    def test_dynamic_weights_only_use_supplied_past_rows(self):
        rules = ["night_futures_direction", "us_market_direction", "sox_tsm_adr_resonance", "vix_risk_off"]
        row = {
            "target_date": "2026-01-02", "next_taiex_return": "1",
            "night_futures_change": "1", "sp500_change_percent": "1", "nasdaq_change_percent": "1",
            "sox_change_percent": "1", "tsm_adr_change_percent": "1", "vix_change_percent": "-3",
        }
        weights, quality = dynamic_weights([row] * 30, rules)
        self.assertAlmostEqual(sum(weights.values()), 100)
        self.assertTrue(all(quality[key]["sample_size"] == 30 for key in rules))

    def test_daily_history_upsert_does_not_duplicate_feature_date(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            market_path, history_path = root / "market.csv", root / "forecast.csv"
            with market_path.open("w", encoding="utf-8", newline="") as stream:
                writer = csv.DictWriter(stream, fieldnames=("trade_date", "taiex_close"))
                writer.writeheader(); writer.writerow({"trade_date": "2026-09-04", "taiex_close": "100"})
            payload = {
                "feature_date": "2026-09-04", "target_date": "2026-09-07", "score": 85,
                "direction": "Strong Bullish", "confidence_status": "extreme", "weights": {"a": 100},
                "contributions": {"a": {"signal": 1}}, "updated_at": "2026-09-04T08:00:00Z",
            }
            with patch("update_rule_forecast.MARKET_HISTORY_PATH", market_path), patch("update_rule_forecast.HISTORY_PATH", history_path):
                self.assertEqual(update_history(payload), (1, 0))
                self.assertEqual(update_history(payload), (1, 0))
            with history_path.open("r", encoding="utf-8", newline="") as stream:
                self.assertEqual(len(list(csv.DictReader(stream))), 1)


if __name__ == "__main__":
    unittest.main()
