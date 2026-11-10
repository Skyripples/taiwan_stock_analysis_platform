from __future__ import annotations

import copy
import hashlib
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pyarrow as pa
import pyarrow.parquet as pq

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from research.futures_binary import dataset as module

TAIPEI = ZoneInfo("Asia/Taipei")


def sample(day=date(2025, 12, 31), value=0.01):
    prediction = datetime.combine(day, datetime.min.time(), TAIPEI).replace(hour=8, minute=30)
    target_day = day + timedelta(days=2)
    return {
        "prediction_date": day, "prediction_at": prediction,
        "tx_available_at": prediction - timedelta(hours=16),
        "max_feature_available_at": prediction - timedelta(hours=1),
        "target_trading_date": target_day,
        "target_available_at": datetime.combine(target_day, datetime.min.time(), TAIPEI).replace(hour=14),
        "next_trading_day_regular_return": value,
        "direction_class": "neutral", "oi_pcr": None,
        "quality_flags": '{"optional_missing":true}',
    }


class FuturesBinaryDatasetTests(unittest.TestCase):
    def test_binary_boundaries_and_missing(self):
        for value, expected in [(0.1, 1), (-0.1, 0), (0, 0), (-0.0, 0), (None, None)]:
            self.assertEqual(module.binary_target(value), expected)

    def test_nonfinite_or_wrong_type_target_rejected(self):
        for value in [float("nan"), float("inf"), -float("inf"), True, "0.01"]:
            with self.assertRaises(ValueError):
                module.binary_target(value)

    def test_preserves_raw_schema_values_missingness_and_input(self):
        source = sample(value=None)
        source["target_available_at"] = None
        before = copy.deepcopy(source)
        result = module.prepare_rows([source])[0]
        self.assertEqual(source, before)
        self.assertIsNone(result[module.BINARY_COLUMN])
        self.assertIsNone(result["oi_pcr"])
        self.assertEqual(result["direction_class"], "neutral")
        self.assertEqual(set(result), set(source) | {module.BINARY_COLUMN})

    def test_duplicate_dates_fail(self):
        with self.assertRaisesRegex(ValueError, "Duplicate prediction_date"):
            module.prepare_rows([sample(), sample()])

    def test_wrong_cutoff_or_date_fails(self):
        for offset in [timedelta(seconds=1), timedelta(days=1)]:
            row = sample()
            row["prediction_at"] += offset
            with self.assertRaisesRegex(ValueError, "08:30"):
                module.prepare_rows([row])

    def test_utc_cutoff_accepted(self):
        row = sample()
        row["prediction_at"] = row["prediction_at"].astimezone(timezone.utc)
        self.assertEqual(len(module.prepare_rows([row])), 1)

    def test_naive_and_missing_required_timestamps_fail(self):
        for key in ["prediction_at", "tx_available_at", "max_feature_available_at", "target_available_at"]:
            for naive in [None, datetime(2026, 1, 2, 14)]:
                row = sample()
                row[key] = naive
                with self.assertRaises(ValueError):
                    module.prepare_rows([row])

    def test_every_feature_timestamp_checked(self):
        row = sample()
        row["sox_available_at"] = row["prediction_at"] + timedelta(microseconds=1)
        with self.assertRaisesRegex(ValueError, "sox_available_at"):
            module.prepare_rows([row])

    def test_feature_at_cutoff_accepted_and_max_not_understated(self):
        row = sample()
        row["sox_available_at"] = row["prediction_at"]
        with self.assertRaisesRegex(ValueError, "understates"):
            module.prepare_rows([row])
        row["max_feature_available_at"] = row["prediction_at"]
        self.assertEqual(len(module.prepare_rows([row])), 1)

    def test_bad_target_date_and_availability_fail(self):
        for key, value in [("target_trading_date", None),
                           ("target_trading_date", date(2025, 12, 31)),
                           ("target_available_at", sample()["prediction_at"])]:
            row = sample()
            row[key] = value
            with self.assertRaises(ValueError):
                module.prepare_rows([row])

    def test_nonfinite_optional_feature_fails_without_dropping_rows(self):
        row = sample()
        row["oi_pcr"] = float("nan")
        with self.assertRaisesRegex(ValueError, "Nonfinite"):
            module.prepare_rows([row])

    def test_train_availability_boundary_across_holiday_and_year(self):
        early = sample(date(2025, 12, 30))
        late = sample(date(2025, 12, 31))
        missing = sample(date(2025, 12, 29), None)
        missing["target_available_at"] = None
        fit = late["target_available_at"] - timedelta(microseconds=1)
        rows = module.prepare_rows([late, missing, early])
        self.assertEqual([r["prediction_date"] for r in module.training_rows_as_of(rows, fit)],
                         [early["prediction_date"]])
        self.assertEqual(len(module.training_rows_as_of(rows, late["target_available_at"])), 2)
        self.assertEqual(module.training_rows_as_of(rows, early["prediction_at"]), [])

    def test_missing_required_and_existing_binary_column_rejected(self):
        row = sample()
        del row["target_available_at"]
        with self.assertRaisesRegex(ValueError, "Missing required"):
            module.prepare_rows([row])
        row = sample()
        row[module.BINARY_COLUMN] = 1
        with self.assertRaisesRegex(ValueError, "already contain"):
            module.prepare_rows([row])

    def test_parquet_partition_read_is_sorted_and_read_only(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for year, row in [(2026, sample(date(2026, 1, 2))), (2025, sample())]:
                path = root / f"partition_year={year}" / "part-00000.parquet"
                path.parent.mkdir()
                pq.write_table(pa.Table.from_pylist([row]), path)
            paths = list(root.rglob("*.parquet"))
            before = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
            rows = module.load_dataset(root)
            self.assertEqual([r["prediction_date"].year for r in rows], [2025, 2026])
            self.assertEqual(before, {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in paths})
            self.assertEqual(set(root.rglob("*.parquet")), set(paths))

    def test_parquet_schema_mismatch_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pq.write_table(pa.Table.from_pylist([sample()]), root / "a.parquet")
            row = sample(date(2026, 1, 2))
            row["new_column"] = 1
            pq.write_table(pa.Table.from_pylist([row]), root / "b.parquet")
            with self.assertRaisesRegex(ValueError, "schema mismatch"):
                module.load_dataset(root)

    def test_existing_v46_schema_is_accepted_unchanged(self):
        from research.build_futures_research_dataset import output_schema

        schema = output_schema()
        row = sample()
        row["partition_year"] = row["prediction_date"].year
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "canonical.parquet"
            pq.write_table(pa.Table.from_pylist([row], schema=schema), path)
            stored = module.load_dataset(directory)[0]
            self.assertEqual(stored[module.BINARY_COLUMN], 1)
            self.assertEqual(stored["direction_class"], "neutral")
            self.assertEqual(pq.ParquetFile(path).schema_arrow, schema)

    def test_empty_parquet_with_naive_timestamp_schema_rejected(self):
        table = pa.Table.from_pylist([sample()]).slice(0, 0)
        schema = table.schema.set(table.schema.get_field_index("prediction_at"),
                                  pa.field("prediction_at", pa.timestamp("us")))
        with tempfile.TemporaryDirectory() as directory:
            pq.write_table(table.cast(schema), Path(directory) / "bad.parquet")
            with self.assertRaisesRegex(ValueError, "Invalid schema type"):
                module.load_dataset(directory)

    def test_empty_directory_and_empty_parquet_fail(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaises(FileNotFoundError):
                module.load_dataset(root)
            table = pa.Table.from_pylist([sample()]).slice(0, 0)
            pq.write_table(table, root / "empty.parquet")
            with self.assertRaisesRegex(ValueError, "no rows"):
                module.load_dataset(root)


if __name__ == "__main__":
    unittest.main()
