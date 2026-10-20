from __future__ import annotations

import gzip
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import import_stock_history as importer


class FakeCopy:
    def __init__(self, connection):
        self.connection = connection

    def __enter__(self):
        self.connection.current_rows = []
        return self

    def __exit__(self, *args):
        return False

    def write_row(self, row):
        self.connection.current_rows.append(row)


class FakeTransaction:
    def __init__(self, connection):
        self.connection = connection

    def __enter__(self):
        return self

    def __exit__(self, exc_type, *_):
        if exc_type is None:
            self.connection.commits += 1
        else:
            self.connection.rollbacks += 1
        return False


class FakeCursor:
    def __init__(self, connection):
        self.connection = connection
        self.result = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, sql):
        self.connection.executed.append(sql)
        if sql.startswith("WITH matched_raw"):
            self.result = self.connection.merge_results.pop(0)
            if isinstance(self.result, Exception):
                raise self.result

    def copy(self, sql):
        self.connection.copy_statements.append(sql)
        return FakeCopy(self.connection)

    def fetchone(self):
        return self.result


class FakeConnection:
    def __init__(self, merge_results):
        self.merge_results = list(merge_results)
        self.executed = []
        self.copy_statements = []
        self.current_rows = []
        self.transactions = 0
        self.commits = 0
        self.rollbacks = 0

    def transaction(self):
        self.transactions += 1
        return FakeTransaction(self)

    def cursor(self):
        return FakeCursor(self)


class StockHistoryBulkImportTests(unittest.TestCase):
    def test_revenue_contract_keeps_canonical_provenance(self):
        table, date_field, fields = importer.SPECS["revenue"]
        self.assertEqual(table, "stock_monthly_revenue")
        self.assertEqual(date_field, "revenue_month")
        self.assertEqual(fields, ("revenue", "revenue_yoy", "revenue_mom"))
        path = next((ROOT / "data_lake/staging/stock_history/revenue").glob("*.jsonl.gz"))
        with gzip.open(path, "rt", encoding="utf-8") as stream:
            row = json.loads(next(line for line in stream if line.strip()))
        self.assertTrue({"source", "source_date", "fetched_at"}.issubset(row))

    def test_batches_stream_without_dropping_rows(self):
        self.assertEqual(list(importer.batches(iter(range(5)), 2)), [[0, 1], [2, 3], [4]])

    def test_all_datasets_use_copy_join_and_expected_conflict_keys(self):
        expected = {
            "revenue": "ON CONFLICT (stock_id,revenue_month)",
            "financials": "ON CONFLICT (stock_id,fiscal_year,quarter,statement_scope)",
            "valuation": "ON CONFLICT (stock_id,valuation_date)",
            "chips": "ON CONFLICT (stock_id,trade_date)",
        }
        for dataset, conflict in expected.items():
            with self.subTest(dataset=dataset):
                self.assertTrue(importer.copy_sql(dataset).startswith("COPY _stock_history_import_"))
                sql = importer.merge_sql(dataset)
                self.assertIn("JOIN stocks s ON s.symbol=st.symbol", sql)
                self.assertIn(conflict, sql)
                self.assertIn("source_payload", sql)

    def test_precedence_rule_is_preserved_for_every_dataset(self):
        for dataset, (table, _, _) in importer.SPECS.items():
            sql = importer.merge_sql(dataset)
            self.assertIn(
                f"EXCLUDED.source_date IS NOT NULL AND ({table}.source_date IS NULL OR EXCLUDED.source_date >= {table}.source_date)",
                sql,
            )
            self.assertIn(
                f"EXCLUDED.source_date IS NULL AND {table}.source_date IS NULL AND ({table}.fetched_at IS NULL OR EXCLUDED.fetched_at >= {table}.fetched_at)",
                sql,
            )

    def test_source_date_and_fetched_at_precedence_truth_table(self):
        allows = importer.precedence_allows
        self.assertTrue(allows(None, None, "2026-01-01", "2026-01-02T00:00:00+00:00"))
        self.assertTrue(allows("2026-01-01", None, "2026-01-01", None))
        self.assertFalse(allows("2026-01-02", None, "2026-01-01", "2026-01-03T00:00:00+00:00"))
        self.assertFalse(allows("2026-01-01", None, None, "2026-01-03T00:00:00+00:00"))
        self.assertTrue(allows(None, "2026-01-01T00:00:00+00:00", None, "2026-01-02T00:00:00+00:00"))
        self.assertFalse(allows(None, "2026-01-02T00:00:00+00:00", None, "2026-01-01T00:00:00+00:00"))

    def test_copy_pipeline_streams_chunks_and_reports_unknown_symbol(self):
        fixture = [
            {"symbol": "2330", "trade_date": "2026-01-02", "foreign_net": 1},
            {"symbol": "UNKNOWN", "trade_date": "2026-01-02", "foreign_net": 2},
            {"symbol": "2454", "trade_date": "2026-01-03", "foreign_net": 3},
        ]
        connection = FakeConnection([
            {"matched_stocks": 1, "inserted_or_updated": 1, "unmatched_symbols": ["UNKNOWN"]},
            {"matched_stocks": 1, "inserted_or_updated": 1, "unmatched_symbols": []},
        ])
        with patch.object(importer, "rows", return_value=iter(fixture)):
            result = importer.import_dataset(connection, "chips", 2)
        self.assertEqual(result["processed"], 3)
        self.assertEqual(result["matched_stocks"], 2)
        self.assertEqual(result["unmatched_symbols"], 1)
        self.assertEqual(result["unmatched_symbol_values"], ["UNKNOWN"])
        self.assertEqual(result["inserted_or_updated"], 2)
        self.assertEqual(len(connection.copy_statements), 2)
        self.assertEqual(connection.transactions, 3)
        self.assertEqual(connection.current_rows[0][-1].obj, fixture[-1])
        self.assertFalse(any("SELECT stock_id FROM stocks WHERE symbol=%s" in sql for sql in connection.executed))

    def test_idempotent_rerun_can_report_zero_affected(self):
        fixture = [{"symbol": "2330", "valuation_date": "2026-01-02", "pe": 20}]
        connection = FakeConnection([
            {"matched_stocks": 1, "inserted_or_updated": 1, "unmatched_symbols": []},
            {"matched_stocks": 1, "inserted_or_updated": 0, "unmatched_symbols": []},
        ])
        with patch.object(importer, "rows", side_effect=lambda _: iter(fixture)):
            first = importer.import_dataset(connection, "valuation", 100)
            second = importer.import_dataset(connection, "valuation", 100)
        self.assertEqual(first["inserted_or_updated"], 1)
        self.assertEqual(second["inserted_or_updated"], 0)

    def test_failed_merge_rolls_back_copy_chunk(self):
        fixture = [{"symbol": "2330", "revenue_month": "2026-01-01", "revenue": 1}]
        connection = FakeConnection([RuntimeError("merge failed")])
        with patch.object(importer, "rows", return_value=iter(fixture)):
            with self.assertRaisesRegex(RuntimeError, "merge failed"):
                importer.import_dataset(connection, "revenue", 100)
        self.assertEqual(connection.commits, 1)  # TEMP table creation only
        self.assertEqual(connection.rollbacks, 1)  # COPY and merge share this transaction

    def test_revenue_repair_migration_is_additive_and_scoped(self):
        sql = (ROOT / "scripts/database/migrations/013_stock_revenue_metadata.sql").read_text(encoding="utf-8")
        self.assertEqual(sql.count("ALTER TABLE stock_monthly_revenue"), 3)
        self.assertEqual(sql.count("ADD COLUMN IF NOT EXISTS"), 3)
        for column in ("source text", "source_date date", "fetched_at timestamptz"):
            self.assertIn(column, sql)
        for table in ("stock_valuations", "stock_chips", "stock_financials"):
            self.assertNotIn(table, sql)


if __name__ == "__main__":
    unittest.main()
