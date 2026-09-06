from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from update_history import validate_prediction_dates


class PredictionDateAlignmentTests(unittest.TestCase):
    def dates(self) -> dict[str, str]:
        return {
            "vix": "2026-09-04",
            "kospi": "2026-09-04",
            "night_futures": "2026-09-04",
        }

    def test_friday_sources_are_valid_for_monday_target(self):
        self.assertEqual(
            validate_prediction_dates(self.dates(), "2026-09-04"),
            "2026-09-07",
        )

    def test_vix_on_target_date_is_rejected(self):
        dates = self.dates()
        dates["vix"] = "2026-09-07"
        with self.assertRaisesRegex(ValueError, "VIX trade date must precede"):
            validate_prediction_dates(dates, "2026-09-04")

    def test_kospi_on_target_date_is_rejected(self):
        dates = self.dates()
        dates["kospi"] = "2026-09-07"
        with self.assertRaisesRegex(ValueError, "KOSPI trade date must precede"):
            validate_prediction_dates(dates, "2026-09-04")

    def test_night_futures_after_target_date_is_rejected(self):
        dates = self.dates()
        dates["night_futures"] = "2026-09-08"
        with self.assertRaisesRegex(ValueError, "between feature and target"):
            validate_prediction_dates(dates, "2026-09-04")


if __name__ == "__main__":
    unittest.main()
