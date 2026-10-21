from __future__ import annotations

import argparse
import json
import sys
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from database.global_market_repository import CONFLICT_KEYS
from providers.yahoo_provider import (
    YahooNasdaqProvider,
    YahooProvider,
    YahooSoxProvider,
    YahooSp500Provider,
    YahooUsdtwdProvider,
)
from research import backfill_global_market as backfill
from research.global_market_alignment import (
    fx_available_at,
    is_available_at_prediction,
    treasury_available_at,
    us_session_available_at,
)


UTC = timezone.utc
EXPECTED_INSTRUMENTS = {
    "SOX": "^SOX",
    "TSM_ADR": "TSM",
    "NASDAQ": "^IXIC",
    "SP500": "^GSPC",
    "USDTWD": "TWD=X",
}


class GlobalMarketFactorBackfillTests(unittest.TestCase):
    def test_cli_defaults_to_local_storage(self):
        with patch.object(sys, "argv", ["backfill_global_market.py", "--symbol", "SOX"]):
            self.assertEqual(backfill.arguments().storage, "local")

    def test_existing_providers_and_canonical_registry_cover_six_series(self):
        provider_symbols = {
            YahooSoxProvider.symbol,
            YahooProvider.symbol,
            YahooNasdaqProvider.symbol,
            YahooSp500Provider.symbol,
            YahooUsdtwdProvider.symbol,
        }
        self.assertEqual(provider_symbols, set(EXPECTED_INSTRUMENTS.values()))
        instruments = {item.symbol: item for item in backfill.INSTRUMENTS}
        self.assertEqual(
            {key: instruments[key].source_symbol for key in EXPECTED_INSTRUMENTS},
            EXPECTED_INSTRUMENTS,
        )
        self.assertIn("DGS10", backfill.SERIES)

        args = argparse.Namespace(all=False, category=None, symbol=[*EXPECTED_INSTRUMENTS, "DGS10"])
        selected_instruments, selected_series = backfill.selected(args)
        self.assertEqual({item.symbol for item in selected_instruments}, set(EXPECTED_INSTRUMENTS))
        self.assertEqual(selected_series, ["DGS10"])

    def test_canonical_keys_are_unique_and_reruns_use_upsert_keys(self):
        symbols = [item.symbol for item in backfill.INSTRUMENTS]
        source_keys = [(item.source, item.source_symbol) for item in backfill.INSTRUMENTS]
        self.assertEqual(len(symbols), len(set(symbols)))
        self.assertEqual(len(source_keys), len(set(source_keys)))
        self.assertEqual(len(backfill.SERIES), len(set(backfill.SERIES)))
        self.assertEqual(CONFLICT_KEYS["market_daily_prices"], ("instrument_id", "trade_date"))
        self.assertEqual(
            CONFLICT_KEYS["macro_observations"],
            ("macro_series_id", "observation_date", "available_at"),
        )

    def test_existing_ndx_and_vix_mappings_are_unchanged(self):
        instruments = {item.symbol: item for item in backfill.INSTRUMENTS}
        self.assertEqual((instruments["NDX"].source_symbol, instruments["NDX"].source), ("^NDX", "yahoo"))
        self.assertEqual((instruments["VIX"].source_symbol, instruments["VIX"].source), ("VIX", "cboe"))

    def test_market_and_macro_availability_obey_dst_and_close_semantics(self):
        standard = date(2026, 1, 15)
        daylight = date(2026, 7, 15)

        self.assertEqual(us_session_available_at(standard).astimezone(UTC).hour, 21)
        self.assertEqual(us_session_available_at(daylight).astimezone(UTC).hour, 20)
        self.assertEqual(fx_available_at(standard).astimezone(UTC).strftime("%H:%M"), "22:05")
        self.assertEqual(fx_available_at(daylight).astimezone(UTC).strftime("%H:%M"), "21:05")
        self.assertEqual(treasury_available_at(standard).astimezone(UTC).hour, 21)
        self.assertEqual(treasury_available_at(daylight).astimezone(UTC).hour, 20)

        for observed, available in (
            (standard, us_session_available_at(standard)),
            (standard, fx_available_at(standard)),
            (standard, treasury_available_at(standard)),
        ):
            observed_at = datetime.combine(observed, datetime.min.time(), available.tzinfo)
            self.assertGreater(available, observed_at)

    def test_pit_join_never_uses_data_after_prediction_cutoff(self):
        cutoff = datetime(2026, 7, 16, 9, 0, tzinfo=backfill.ZoneInfo("Asia/Taipei"))
        prior_us_close = us_session_available_at(date(2026, 7, 15))
        same_day_us_close = us_session_available_at(date(2026, 7, 16))
        self.assertTrue(is_available_at_prediction(prior_us_close, cutoff))
        self.assertFalse(is_available_at_prediction(same_day_us_close, cutoff))
        self.assertTrue(is_available_at_prediction(cutoff, cutoff))

    def test_raw_canonical_rows_keep_level_source_fetch_and_pit_metadata(self):
        class Response:
            text = "observation_date,DGS10\n2026-07-15,4.20\n"

        class Client:
            def get(self, *args, **kwargs):
                return Response()

        rows = backfill.fetch_fred(Client(), "DGS10", date(2026, 7, 15), date(2026, 7, 15))
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["value"], 4.2)
        self.assertEqual(row["observation_date"], "2026-07-15")
        self.assertEqual(row["metadata"]["source"], "fred")
        self.assertEqual(row["metadata"]["source_series_id"], "DGS10")
        self.assertIn("fetched_at", row["metadata"])
        self.assertGreater(datetime.fromisoformat(row["available_at"]), datetime(2026, 7, 15, tzinfo=UTC))

    def test_local_mode_needs_no_database_and_rerun_is_idempotent(self):
        instrument = next(item for item in backfill.INSTRUMENTS if item.symbol == "SOX")
        fetched = "2026-07-16T01:00:00+00:00"
        row = {
            "trade_date": "2026-07-15", "open": 5000.0, "high": 5100.0, "low": 4990.0,
            "close": 5075.0, "adjusted_close": 5075.0, "volume": 123.0,
            "available_at": "2026-07-15T20:30:00+00:00", "source_updated_at": None,
            "quality_flags": {}, "metadata": {"source": "yahoo", "source_symbol": "^SOX",
                "availability_rule": "us", "fetched_at": fetched},
        }
        options = argparse.Namespace(
            all=False, category=None, symbol=["SOX"], start=date(2026, 7, 15), end=date(2026, 7, 15),
            resume=True, dry_run=False, storage="local", init_schema=False,
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "global_market"
            report = Path(directory) / "report.json"
            checkpoint = root / "_checkpoints" / "global_market_backfill.json"
            with (
                patch.object(backfill, "LAKE_ROOT", root),
                patch.object(backfill, "LOCAL_CHECKPOINT", checkpoint),
                patch.object(backfill, "REPORT", report),
                patch.object(backfill, "arguments", return_value=options),
                patch.object(backfill, "fetch_yahoo", return_value=[row]),
                patch.object(backfill, "configured", side_effect=AssertionError("database must not be inspected")),
            ):
                self.assertEqual(backfill.main(), 0)
                self.assertEqual(backfill.main(), 0)

            import pyarrow.parquet as pq
            path = root / "daily_prices/category=market/partition_year=2026/part-00000.parquet"
            rows = pq.ParquetFile(path).read().to_pylist()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["canonical_symbol"], instrument.symbol)
            self.assertEqual(rows[0]["source"], "yahoo")
            self.assertEqual(rows[0]["fetched_at"], datetime.fromisoformat(fetched))
            saved = json.loads(checkpoint.read_text(encoding="utf-8"))
            self.assertEqual(saved["tasks"]["market:SOX"]["last_cursor"], "2026-07-15")

    def test_local_macro_uses_existing_partition_shape_and_deduplicates(self):
        fetched = "2026-07-16T01:00:00+00:00"
        row = {
            "observation_date": "2026-07-15", "available_at": "2026-07-15T20:00:00+00:00",
            "value": 4.2, "value_text": None, "unit": "Percent", "vintage_date": "2026-07-15",
            "preliminary": False, "source_updated_at": None,
            "metadata": {"source": "fred", "source_series_id": "DGS10", "fetched_at": fetched,
                "availability_rule": "conservative 16:00 ET same-day publication"},
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            backfill.write_local_macro("DGS10", [row], root)
            backfill.write_local_macro("DGS10", [row], root)
            import pyarrow.parquet as pq
            path = root / "macro_observations/series_key=DGS10/partition_year=2026/part-00000.parquet"
            rows = pq.ParquetFile(path).read().to_pylist()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["series_key"], "DGS10")
            self.assertEqual(rows[0]["value"], 4.2)
            self.assertEqual(rows[0]["source"], "fred")
            self.assertIn("fetched_at", rows[0])

    def test_daily_eligibility_requires_fetch_at_or_after_availability(self):
        base = {
            "available_at": "2026-10-18T21:05:00+00:00",
            "metadata": {"fetched_at": "2026-10-18T18:39:32+00:00"},
        }
        self.assertFalse(backfill.daily_row_is_closed(base))
        base["metadata"]["fetched_at"] = "2026-10-18T21:05:00+00:00"
        self.assertTrue(backfill.daily_row_is_closed(base))
        base["metadata"]["fetched_at"] = "2026-10-18T21:05:01+00:00"
        self.assertTrue(backfill.daily_row_is_closed(base))

    def test_fx_current_session_is_excluded_and_marked(self):
        class FrozenDatetime(datetime):
            current = datetime(2026, 10, 18, 18, 39, 32, tzinfo=UTC)

            @classmethod
            def now(cls, tz=None):
                return cls.current.astimezone(tz) if tz else cls.current.replace(tzinfo=None)

        class Response:
            def json(self):
                timestamp = int(datetime(2026, 10, 18, 4, 0, tzinfo=UTC).timestamp())
                quote = {key: [value] for key, value in {
                    "open": 32.4, "high": 32.5, "low": 32.3, "close": 32.42, "volume": 1.0,
                }.items()}
                return {"chart": {"result": [{"meta": {"exchangeTimezoneName": "America/New_York"},
                    "timestamp": [timestamp], "indicators": {"quote": [quote], "adjclose": [{"adjclose": [32.42]}]}}]}}

        class Client:
            def get(self, *args, **kwargs):
                return Response()

        instrument = next(item for item in backfill.INSTRUMENTS if item.symbol == "USDTWD")
        skipped = []
        with patch.object(backfill, "datetime", FrozenDatetime):
            rows = backfill.fetch_yahoo(Client(), instrument, date(2026, 10, 18), date(2026, 10, 18), skipped)
        self.assertEqual(rows, [])
        self.assertEqual(skipped[0]["status"], "current_session")
        self.assertEqual(skipped[0]["reason"], "not_closed")

        FrozenDatetime.current = datetime(2026, 10, 18, 21, 5, tzinfo=UTC)
        skipped = []
        with patch.object(backfill, "datetime", FrozenDatetime):
            rows = backfill.fetch_yahoo(Client(), instrument, date(2026, 10, 18), date(2026, 10, 18), skipped)
        self.assertEqual(len(rows), 1)
        self.assertEqual(skipped, [])

    def test_resume_uses_three_day_overlap_without_expanding_full_history(self):
        checkpoint = {"last_cursor": "2026-10-19"}
        self.assertEqual(
            backfill.resume_start(date(2021, 10, 13), checkpoint, local=True),
            date(2026, 10, 16),
        )
        self.assertEqual(
            backfill.resume_start(date(2021, 10, 13), checkpoint, local=False),
            date(2026, 10, 19),
        )

    def test_overlap_cleanup_removes_only_ineligible_selected_symbol(self):
        instrument = next(item for item in backfill.INSTRUMENTS if item.symbol == "USDTWD")
        valid = {
            "trade_date": "2026-10-16", "open": 32.0, "high": 32.2, "low": 31.9, "close": 32.1,
            "adjusted_close": 32.1, "volume": 1.0, "available_at": "2026-10-16T21:05:00+00:00",
            "source_updated_at": None, "quality_flags": {},
            "metadata": {"source": "yahoo", "source_symbol": "TWD=X", "fetched_at": "2026-10-17T00:00:00+00:00"},
        }
        invalid = {**valid, "trade_date": "2026-10-18", "available_at": "2026-10-18T21:05:00+00:00",
            "metadata": {**valid["metadata"], "fetched_at": "2026-10-18T18:39:32+00:00"}}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            backfill.write_local_daily(instrument, [valid], root)
            # Seed the prior buggy row directly to exercise generic live-edge cleanup.
            path = root / "daily_prices/category=fx/partition_year=2026/part-00000.parquet"
            current = backfill._parquet_rows(path)
            metadata = invalid["metadata"]
            current.append({
                **current[0], "trade_date": date(2026, 10, 18),
                "available_at": datetime.fromisoformat(invalid["available_at"]),
                "fetched_at": datetime.fromisoformat(metadata["fetched_at"]),
                "metadata": json.dumps(metadata),
            })
            backfill._write_parquet_atomic(path, current, backfill._daily_schema(), ["category", "canonical_symbol", "source"])

            result = backfill.write_local_daily(
                instrument, [valid], root, cleanup_start=date(2026, 10, 16), cleanup_end=date(2026, 10, 19),
            )
            import pyarrow.parquet as pq
            rows = pq.ParquetFile(path).read().to_pylist()
            self.assertEqual(result["removed_ineligible"], 1)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["trade_date"], date(2026, 10, 16))
            self.assertEqual(rows[0]["close"], 32.1)


if __name__ == "__main__":
    unittest.main()
