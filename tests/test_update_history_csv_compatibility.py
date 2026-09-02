from __future__ import annotations

import csv
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import update_history
from build_prediction_dataset import build_prediction_dataset


PRODUCTION_MARKET_HEADER = (
    "trade_date", "taiwan_market_trade_date", "institutional_trade_date",
    "foreign_futures_trade_date", "night_futures_trade_date", "tsm_adr_trade_date",
    "sox_trade_date", "sp500_trade_date", "nasdaq_trade_date", "taiex_close",
    "taiex_change_percent", "tpex_close", "turnover", "advancing", "declining",
    "unchanged", "foreign_cash_flow", "foreign_futures_position",
    "night_futures_change", "tsm_adr_change_percent", "sox_change_percent",
    "sp500_change_percent", "nasdaq_change_percent", "vix_trade_date",
    "vix_change_percent", "kospi_trade_date", "kospi_change_percent",
)


class MarketHistoryCsvCompatibilityTests(unittest.TestCase):
    def _row(self, fields: tuple[str, ...], trade_date: str, marker: str) -> dict[str, str]:
        return {
            field: trade_date if field == "trade_date" or field.endswith("_trade_date") else marker
            for field in fields
        }

    def _write_csv(self, path: Path, fields: tuple[str, ...], rows: list[dict[str, str]]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8", newline="") as output:
            writer = csv.DictWriter(output, fieldnames=fields, lineterminator="\n")
            writer.writeheader()
            writer.writerows(rows)

    def _valid_market_row(self, trade_date: str, night_date: str, close: str) -> dict[str, str]:
        row = {field: "1" for field in PRODUCTION_MARKET_HEADER}
        row["trade_date"] = trade_date
        for field in update_history.SOURCE_DATE_FIELDS:
            row[field] = trade_date
        row["night_futures_trade_date"] = night_date
        row["vix_trade_date"] = trade_date
        row["kospi_trade_date"] = trade_date
        row["taiex_close"] = close
        row["tpex_close"] = "250"
        return row

    def test_existing_production_header_supports_insert_and_same_day_update(self):
        self.assertEqual(update_history.MARKET_FIELDS, PRODUCTION_MARKET_HEADER)
        with tempfile.TemporaryDirectory() as folder:
            history_dir = Path(folder)
            market_path = history_dir / "market_daily.csv"
            signals_path = history_dir / "signals_daily.csv"
            old_market = self._row(update_history.MARKET_FIELDS, "2026-08-14", "old")
            old_signals = self._row(update_history.SIGNALS_FIELDS, "2026-08-14", "old")
            self._write_csv(market_path, PRODUCTION_MARKET_HEADER, [old_market])
            self._write_csv(signals_path, update_history.SIGNALS_FIELDS, [old_signals])

            new_market = self._row(update_history.MARKET_FIELDS, "2026-09-01", "first")
            new_signals = self._row(update_history.SIGNALS_FIELDS, "2026-09-01", "first")
            with (
                patch.object(update_history, "load_sources", return_value={}),
                patch.object(update_history, "build_history_rows", return_value=(new_market, new_signals)),
            ):
                update_history.update_history(history_dir=history_dir)

            updated_market = self._row(update_history.MARKET_FIELDS, "2026-09-01", "updated")
            updated_signals = self._row(update_history.SIGNALS_FIELDS, "2026-09-01", "updated")
            with (
                patch.object(update_history, "load_sources", return_value={}),
                patch.object(update_history, "build_history_rows", return_value=(updated_market, updated_signals)),
            ):
                update_history.update_history(history_dir=history_dir)

            with market_path.open("r", encoding="utf-8", newline="") as source:
                reader = csv.DictReader(source)
                rows = list(reader)
                self.assertEqual(tuple(reader.fieldnames or ()), update_history.MARKET_FIELDS)
            self.assertEqual([row["trade_date"] for row in rows], ["2026-08-14", "2026-09-01"])
            self.assertEqual(rows[0]["taiex_close"], "old")
            self.assertEqual(rows[1]["taiex_close"], "updated")

    def test_production_header_continues_into_prediction_dataset(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            history_path = root / "market_daily.csv"
            dataset_path = root / "prediction_dataset.csv"
            rows = [
                self._valid_market_row("2026-08-31", "2026-09-01", "24000"),
                self._valid_market_row("2026-09-01", "2026-09-02", "24240"),
            ]
            self._write_csv(history_path, PRODUCTION_MARKET_HEADER, rows)
            count = build_prediction_dataset(history_path, dataset_path)
            self.assertEqual(count, 1)
            with dataset_path.open("r", encoding="utf-8", newline="") as source:
                result = list(csv.DictReader(source))
            self.assertEqual(result[0]["feature_date"], "2026-08-31")
            self.assertEqual(result[0]["target_date"], "2026-09-01")
            self.assertEqual(result[0]["next_taiex_return"], "1")


if __name__ == "__main__":
    unittest.main()
