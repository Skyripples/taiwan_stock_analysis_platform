from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from api.routes.stocks import router
from api.services import stocks as service


FIXTURE = [
    {"symbol": "2330", "name": "台積電", "market": "TWSE", "industry": "半導體業", "instrument_type": "company", "close": 1000},
]


class FakeCursor:
    def __init__(self):
        self.query = ""
        self.parameters = ()

    def execute(self, query, parameters=()):
        self.query, self.parameters = query, parameters

    def fetchone(self):
        return {"total": len(FIXTURE), "results": FIXTURE}

    def __enter__(self): return self
    def __exit__(self, *_): return False


class FakeConnection:
    def __init__(self, cursor): self._cursor = cursor
    def cursor(self): return self._cursor
    def __enter__(self): return self
    def __exit__(self, *_): return False


class FakePool:
    def __init__(self): self.cursor = FakeCursor()
    def connection(self): return FakeConnection(self.cursor)


class StockScreenerTests(unittest.TestCase):
    def setUp(self):
        app = FastAPI()
        app.include_router(router, prefix="/api/v1")
        self.client = TestClient(app)

    @patch("api.routes.stocks.screen_stocks")
    def test_no_filters_and_pagination(self, screen):
        screen.return_value = {"total": 2390, "count": 50, "limit": 50, "offset": 0, "results": FIXTURE}
        response = self.client.get("/api/v1/stocks/screener")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["total"], 2390)
        self.assertEqual(screen.call_args.args[2:], ("asc", 50, 0))

    @patch("api.routes.stocks.screen_stocks")
    def test_filters_sort_and_page_are_forwarded(self, screen):
        screen.return_value = {"total": 1, "count": 1, "limit": 20, "offset": 40, "results": FIXTURE}
        response = self.client.get(
            "/api/v1/stocks/screener?market=TWSE&industry=%E5%8D%8A%E5%B0%8E%E9%AB%94%E6%A5%AD"
            "&pe_min=10&pe_max=30&revenue_yoy_min=5&roe_min=10&foreign_5d_min=1"
            "&sort=roe&order=desc&limit=20&offset=40"
        )
        self.assertEqual(response.status_code, 200)
        filters = screen.call_args.args[0]
        self.assertEqual(filters["market"], "TWSE")
        self.assertEqual(filters["industry"], "半導體業")
        self.assertEqual((screen.call_args.args[1], screen.call_args.args[2]), ("roe", "desc"))

    def test_invalid_ranges_and_parameters_return_422(self):
        for query in ("pe_min=30&pe_max=10", "sort=drop_table", "limit=101", "market=NYSE"):
            with self.subTest(query=query):
                self.assertEqual(self.client.get(f"/api/v1/stocks/screener?{query}").status_code, 422)

    @patch("api.routes.stocks.search_stocks", return_value=FIXTURE)
    def test_existing_search_route_is_unchanged(self, _search):
        self.assertEqual(self.client.get("/api/v1/stocks?search=2330").status_code, 200)

    def test_parameterized_sql_and_null_filter_semantics(self):
        fake = FakePool()
        injection = "2330%' OR true --"
        with patch.object(service, "pool", fake):
            result = service.screen_stocks(
                {"search": injection, "market": "TWSE", "pe_min": 1, "revenue_yoy_max": 20,
                 "roe_min": 5, "foreign_5d_min": 0}, "foreign_5d", "desc", 50, 0,
            )
        self.assertEqual(result["count"], 1)
        self.assertNotIn(injection, fake.cursor.query)
        self.assertIn(f"%{injection}%", fake.cursor.parameters)
        self.assertIn("pe>=%s", fake.cursor.query)
        self.assertIn("active=true AND (symbol ILIKE %s OR name ILIKE %s) AND market=%s", fake.cursor.query)
        self.assertIn("revenue_yoy<=%s", fake.cursor.query)
        self.assertIn("foreign_5d>=%s", fake.cursor.query)
        self.assertIn("LIMIT %s OFFSET %s", fake.cursor.query)
        self.assertIn("ORDER BY foreign_5d DESC", fake.cursor.query)
        # SQL comparisons naturally exclude NULL only for enabled filters.
        self.assertNotIn("coalesce(pe", fake.cursor.query.lower())


if __name__ == "__main__":
    unittest.main()
