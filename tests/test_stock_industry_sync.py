from __future__ import annotations

import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from build_all_stocks import DAILY_TABLES, build_industry_code_map, official_industry
from database.repository import StockRepository


class FakeCursor:
    def __init__(self): self.statement = None
    def executemany(self, statement, _rows): self.statement = statement
    def __enter__(self): return self
    def __exit__(self, *_): return False


class FakeConnection:
    def __init__(self): self.last_cursor = None
    def cursor(self): self.last_cursor = FakeCursor(); return self.last_cursor


class StockIndustrySyncTests(unittest.TestCase):
    def test_daily_pipeline_loads_both_official_industry_name_sources(self):
        self.assertIn("twse_revenue", DAILY_TABLES)
        self.assertIn("tpex_revenue", DAILY_TABLES)

    def test_official_name_wins_and_matches_peer_canonical_value(self):
        self.assertEqual(official_industry({"產業別": "半導體業"}, {"industry": "半導體"}), "半導體業")

    def test_transient_empty_source_preserves_existing_value(self):
        self.assertEqual(official_industry({}, {"industry": "其他電子業"}), "其他電子業")
        self.assertEqual(official_industry({"產業別": ""}, {"industry": "半導體業"}), "半導體業")

    def test_official_code_mapping_fills_company_missing_from_revenue_table(self):
        profiles = {"2330": {"產業別": "24"}, "9999": {"產業別": "24"}}
        mapping = build_industry_code_map(profiles, {"2330": {"產業別": "半導體業"}}, "TWSE")
        self.assertEqual(mapping, {"24": "半導體業"})
        self.assertEqual(official_industry({}, None, profiles["9999"], "TWSE", mapping), "半導體業")

    def test_unknown_company_remains_null(self):
        self.assertIsNone(official_industry({}, {}))
        self.assertIsNone(official_industry({"產業別": "資料不足"}, {"industry": None}))

    def test_database_upsert_never_replaces_industry_with_null(self):
        connection = FakeConnection()
        StockRepository(connection).upsert_many("stocks", [{"symbol": "2330", "industry": None}])
        statement = connection.last_cursor.statement.as_string()
        self.assertIn('"industry" = COALESCE(EXCLUDED."industry", "stocks"."industry")', statement)


if __name__ == "__main__":
    unittest.main()
