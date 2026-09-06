from __future__ import annotations

import csv
import sys
import tempfile
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import build_prediction_dataset as builder


class PredictionDatasetDateTests(unittest.TestCase):
    def _row(
        self,
        trade_date: str,
        *,
        close: str = "20000",
        night_date: str | None = None,
        source_overrides: dict[str, str] | None = None,
    ) -> dict[str, str]:
        row = {field: "1" for field in builder.REQUIRED_FEATURE_FIELDS}
        for field in builder.DATE_FIELDS:
            row[field] = trade_date
        row["trade_date"] = trade_date
        row["night_futures_trade_date"] = night_date or trade_date
        row["taiex_close"] = close
        row["tpex_close"] = "250"
        row.update(source_overrides or {})
        return row

    def _build(self, rows: list[dict[str, str]]) -> tuple[int, list[dict[str, str]]]:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source_path = root / "market_daily.csv"
            output_path = root / "prediction_dataset.csv"
            fields = ("trade_date", *builder.REQUIRED_FEATURE_FIELDS)
            with source_path.open("w", encoding="utf-8", newline="") as output:
                writer = csv.DictWriter(output, fieldnames=fields, lineterminator="\n")
                writer.writeheader()
                writer.writerows(rows)
            count = builder.build_prediction_dataset(source_path, output_path)
            with output_path.open("r", encoding="utf-8", newline="") as source:
                result = list(csv.DictReader(source))
        return count, result

    def test_history_gap_is_not_used_as_next_session(self):
        count, rows = self._build(
            [self._row("2026-08-14"), self._row("2026-09-02", close="20100")]
        )
        self.assertEqual(count, 0)
        self.assertEqual(rows, [])

    def test_consecutive_trading_sessions_create_pair(self):
        count, rows = self._build(
            [self._row("2026-09-02"), self._row("2026-09-03", close="20100")]
        )
        self.assertEqual(count, 1)
        self.assertEqual(rows[0]["target_date"], "2026-09-03")

    def test_friday_pairs_with_monday(self):
        count, rows = self._build(
            [self._row("2026-09-04"), self._row("2026-09-07", close="20100")]
        )
        self.assertEqual(count, 1)
        self.assertEqual(rows[0]["target_date"], "2026-09-07")

    def test_night_futures_feature_date_is_valid(self):
        count, _ = self._build(
            [
                self._row("2026-09-02", night_date="2026-09-02"),
                self._row("2026-09-03", close="20100"),
            ]
        )
        self.assertEqual(count, 1)

    def test_night_futures_target_date_is_valid(self):
        count, _ = self._build(
            [
                self._row("2026-09-02", night_date="2026-09-03"),
                self._row("2026-09-03", close="20100"),
            ]
        )
        self.assertEqual(count, 1)

    def test_night_futures_after_target_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "night_futures_trade_date"):
            self._build(
                [
                    self._row("2026-09-02", night_date="2026-09-04"),
                    self._row("2026-09-03", close="20100"),
                ]
            )

    def test_preopen_market_source_on_target_date_is_rejected(self):
        fields = (
            "tsm_adr_trade_date",
            "sox_trade_date",
            "sp500_trade_date",
            "nasdaq_trade_date",
            "vix_trade_date",
            "kospi_trade_date",
        )
        for field in fields:
            with self.subTest(field=field):
                with self.assertRaisesRegex(ValueError, field):
                    self._build(
                        [
                            self._row(
                                "2026-09-02",
                                source_overrides={field: "2026-09-03"},
                            ),
                            self._row("2026-09-03", close="20100"),
                        ]
                    )

    def test_missing_expected_target_is_skipped_without_failure(self):
        count, rows = self._build([self._row("2026-09-02")])
        self.assertEqual(count, 0)
        self.assertEqual(rows, [])


if __name__ == "__main__":
    unittest.main()
