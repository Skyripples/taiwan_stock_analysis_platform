from __future__ import annotations

import csv
import json
import sys
import tempfile
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import update_prediction_history as history


class PredictionHistoryDateTests(unittest.TestCase):
    def _market(self, path: Path, rows: list[tuple[str, float]]) -> None:
        with path.open("w", encoding="utf-8", newline="") as output:
            writer = csv.DictWriter(output, fieldnames=("trade_date", "taiex_close"))
            writer.writeheader()
            for trade_date, close in rows:
                writer.writerow({"trade_date": trade_date, "taiex_close": close})

    def _prediction(self, path: Path, feature_date: str, target_date: str) -> None:
        path.write_text(
            json.dumps(
                {
                    "feature_date": feature_date,
                    "target_date": target_date,
                    "up_probability": 0.6,
                    "down_probability": 0.4,
                    "direction": "up",
                    "confidence": 0.6,
                    "model_version": "test",
                    "generated_at": "2026-09-07T00:00:00Z",
                }
            ),
            encoding="utf-8",
        )

    def _existing(self, path: Path, row: dict[str, str]) -> None:
        complete = {field: "" for field in history.FIELDS}
        complete.update(row)
        with path.open("w", encoding="utf-8", newline="") as output:
            writer = csv.DictWriter(output, fieldnames=history.FIELDS)
            writer.writeheader()
            writer.writerow(complete)

    def _read(self, path: Path) -> list[dict[str, str]]:
        with path.open("r", encoding="utf-8", newline="") as source:
            return list(csv.DictReader(source))

    def test_gap_does_not_settle_with_later_csv_row(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            market, prediction, output = root / "market.csv", root / "prediction.json", root / "history.csv"
            self._market(market, [("2026-08-14", 20000), ("2026-09-02", 21000)])
            self._prediction(prediction, "2026-08-14", "2026-08-17")
            row_count, validated = history.update_prediction_history(prediction, market, output)
            rows = self._read(output)
            self.assertEqual((row_count, validated), (1, 0))
            self.assertEqual(rows[0]["target_date"], "2026-08-17")
            self.assertEqual(rows[0]["hit"], "")

    def test_expected_target_is_settled(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            market, prediction, output = root / "market.csv", root / "prediction.json", root / "history.csv"
            self._market(market, [("2026-08-14", 20000), ("2026-08-17", 20200)])
            self._prediction(prediction, "2026-08-14", "2026-08-17")
            row_count, validated = history.update_prediction_history(prediction, market, output)
            rows = self._read(output)
            self.assertEqual((row_count, validated), (1, 1))
            self.assertEqual(rows[0]["target_close"], "20200")
            self.assertEqual(rows[0]["hit"], "true")

    def test_configured_target_mismatch_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            market, prediction, output = root / "market.csv", root / "prediction.json", root / "history.csv"
            self._market(market, [("2026-08-14", 20000), ("2026-09-02", 21000)])
            self._prediction(prediction, "2026-08-14", "2026-09-02")
            with self.assertRaisesRegex(ValueError, "does not match the next Taiwan trading day"):
                history.update_prediction_history(prediction, market, output)

    def test_friday_settles_on_monday(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            market, prediction, output = root / "market.csv", root / "prediction.json", root / "history.csv"
            self._market(market, [("2026-09-04", 20000), ("2026-09-07", 19900)])
            self._prediction(prediction, "2026-09-04", "2026-09-07")
            _, validated = history.update_prediction_history(prediction, market, output)
            rows = self._read(output)
            self.assertEqual(validated, 1)
            self.assertEqual(rows[0]["target_date"], "2026-09-07")

    def test_validated_row_is_not_processed_again(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            market, prediction, output = root / "market.csv", root / "missing.json", root / "history.csv"
            self._market(market, [("2026-08-14", 20000), ("2026-08-17", 20200)])
            self._existing(
                output,
                {
                    "feature_date": "2026-08-14",
                    "target_date": "2026-08-17",
                    "predicted_direction": "up",
                    "target_close": "20200",
                    "actual_return": "1",
                    "actual_direction": "up",
                    "hit": "true",
                    "validated_at": "2026-08-17T06:00:00Z",
                },
            )
            row_count, validated = history.update_prediction_history(prediction, market, output)
            rows = self._read(output)
            self.assertEqual((row_count, validated), (1, 0))
            self.assertEqual(rows[0]["validated_at"], "2026-08-17T06:00:00Z")


if __name__ == "__main__":
    unittest.main()
