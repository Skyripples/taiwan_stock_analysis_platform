from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from update_history import validate_prediction_dates


class PredictionDateAlignmentTests(unittest.TestCase):
    def dates(self) -> dict[str, str]:
        return {
            "taiwan_market": "2026-09-04",
            "institutional": "2026-09-04",
            "foreign_futures": "2026-09-04",
            "vix": "2026-09-04",
            "kospi": "2026-09-04",
            "night_futures": "2026-09-04",
            "tsm_adr": "2026-09-04",
            "sox": "2026-09-04",
            "sp500": "2026-09-04",
            "nasdaq": "2026-09-04",
        }

    def test_friday_sources_are_valid_for_monday_target(self):
        self.assertEqual(
            validate_prediction_dates(self.dates(), "2026-09-04"),
            "2026-09-07",
        )

    def test_vix_on_target_date_is_rejected(self):
        dates = self.dates()
        dates["vix"] = "2026-09-07"
        with self.assertRaisesRegex(ValueError, "Invalid prediction source date for vix"):
            validate_prediction_dates(dates, "2026-09-04")

    def test_kospi_on_target_date_is_rejected(self):
        dates = self.dates()
        dates["kospi"] = "2026-09-07"
        with self.assertRaisesRegex(ValueError, "Invalid prediction source date for kospi"):
            validate_prediction_dates(dates, "2026-09-04")

    def test_night_futures_after_target_date_is_rejected(self):
        dates = self.dates()
        dates["night_futures"] = "2026-09-08"
        with self.assertRaisesRegex(ValueError, "Invalid prediction source date for night_futures"):
            validate_prediction_dates(dates, "2026-09-04")


if __name__ == "__main__":
    unittest.main()
