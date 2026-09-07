from __future__ import annotations

import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from prediction_temporal import prediction_target_date, source_date_is_valid


class PredictionTemporalPreflightTests(unittest.TestCase):
    def test_normal_consecutive_session(self):
        self.assertEqual(prediction_target_date("2026-09-02"), "2026-09-03")

    def test_friday_to_monday(self):
        self.assertEqual(prediction_target_date("2026-09-04"), "2026-09-07")

    def test_history_gaps_cannot_change_official_target(self):
        # Whether one or many CSV rows are absent, the calendar target is unchanged.
        self.assertEqual(prediction_target_date("2026-08-14"), "2026-08-17")

    def test_pending_target_is_resolved_without_requiring_a_history_row(self):
        self.assertEqual(prediction_target_date("2026-09-04"), "2026-09-07")

    def test_night_futures_feature_and_target_dates_are_valid(self):
        for source_date in ("2026-09-04", "2026-09-07"):
            with self.subTest(source_date=source_date):
                self.assertTrue(
                    source_date_is_valid(
                        "night_futures", source_date, "2026-09-04", "2026-09-07"
                    )
                )

    def test_sources_after_target_are_rejected(self):
        for source in ("night_futures", "tsm_adr", "sox", "sp500", "nasdaq", "vix", "kospi"):
            with self.subTest(source=source):
                self.assertFalse(
                    source_date_is_valid(
                        source, "2026-09-08", "2026-09-04", "2026-09-07"
                    )
                )

    def test_target_day_closes_are_rejected_for_preopen_sources(self):
        for source in ("tsm_adr", "sox", "sp500", "nasdaq", "vix", "kospi"):
            with self.subTest(source=source):
                self.assertFalse(
                    source_date_is_valid(
                        source, "2026-09-07", "2026-09-04", "2026-09-07"
                    )
                )

    def test_stale_source_is_excluded_by_max_age(self):
        self.assertFalse(
            source_date_is_valid(
                "vix",
                "2026-08-01",
                "2026-09-04",
                "2026-09-07",
                max_age_days=4,
            )
        )


if __name__ == "__main__":
    unittest.main()
