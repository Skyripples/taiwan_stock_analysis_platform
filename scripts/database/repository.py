"""Small repository abstraction used by stock JSON -> PostgreSQL sync jobs."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any


CONFLICT_KEYS = {
    "stocks": ("symbol",),
    "stock_quotes": ("stock_id", "trade_date"),
    "stock_valuations": ("stock_id", "valuation_date"),
    "stock_monthly_revenue": ("stock_id", "revenue_month"),
    "stock_financials": ("stock_id", "fiscal_year", "quarter", "statement_scope"),
    "stock_chips": ("stock_id", "trade_date"),
    "stock_health": ("stock_id", "as_of_date", "rules_version", "category", "metric_key"),
    "industry_rankings": ("industry", "metric_key", "comparison_period", "stock_id"),
}


class StockRepository:
    def __init__(self, connection):
        self.connection = connection

    @staticmethod
    def _json_values(row: Mapping[str, Any]) -> dict[str, Any]:
        from psycopg.types.json import Jsonb

        return {
            key: Jsonb(value) if key in {"source_payload", "row_counts"} and value is not None else value
            for key, value in row.items()
        }

    def upsert_many(self, table: str, rows: Iterable[Mapping[str, Any]]) -> int:
        if table not in CONFLICT_KEYS:
            raise ValueError(f"Unsupported table: {table}")
        materialized = [self._json_values(row) for row in rows]
        if not materialized:
            return 0
        columns = tuple(materialized[0])
        if any(tuple(row) != columns for row in materialized):
            raise ValueError(f"Rows for {table} do not share the same columns")
        from psycopg import sql

        conflict = CONFLICT_KEYS[table]
        updates = [name for name in columns if name not in conflict and name != "created_at"]
        assignments = [
            sql.SQL("{} = EXCLUDED.{}").format(sql.Identifier(name), sql.Identifier(name))
            for name in updates
        ]
        assignments.append(sql.SQL("updated_at = now()"))
        comparable = [name for name in updates if name != "updated_at"]
        change_guard = sql.SQL("({current}) IS DISTINCT FROM ({incoming})").format(
            current=sql.SQL(", ").join(
                sql.SQL("{}.{}").format(sql.Identifier(table), sql.Identifier(name))
                for name in comparable
            ),
            incoming=sql.SQL(", ").join(
                sql.SQL("EXCLUDED.{}").format(sql.Identifier(name)) for name in comparable
            ),
        )
        statement = sql.SQL(
            "INSERT INTO {table} ({columns}) VALUES ({values}) "
            "ON CONFLICT ({conflict}) DO UPDATE SET {updates} WHERE {change_guard}"
        ).format(
            table=sql.Identifier(table),
            columns=sql.SQL(", ").join(map(sql.Identifier, columns)),
            values=sql.SQL(", ").join(sql.Placeholder(name) for name in columns),
            conflict=sql.SQL(", ").join(map(sql.Identifier, conflict)),
            updates=sql.SQL(", ").join(assignments),
            change_guard=change_guard,
        )
        with self.connection.cursor() as cursor:
            cursor.executemany(statement, materialized)
        return len(materialized)

    def stock_ids(self, symbols: Iterable[str] | None = None) -> dict[str, int]:
        query = "SELECT symbol, stock_id FROM stocks"
        params: tuple[Any, ...] = ()
        symbols = list(symbols or [])
        if symbols:
            query += " WHERE symbol = ANY(%s)"
            params = (symbols,)
        with self.connection.cursor() as cursor:
            cursor.execute(query, params)
            return {row["symbol"]: row["stock_id"] for row in cursor.fetchall()}

    def start_run(self, mode: str, source_updated_at: str | None) -> int:
        with self.connection.cursor() as cursor:
            cursor.execute(
                """INSERT INTO pipeline_runs(mode, status, source_updated_at)
                   VALUES (%s, 'running', %s) RETURNING run_id""",
                (mode, source_updated_at),
            )
            return cursor.fetchone()["run_id"]

    def finish_run(
        self, run_id: int, status: str, row_counts: Mapping[str, int], error: str | None = None
    ) -> None:
        from psycopg.types.json import Jsonb

        with self.connection.cursor() as cursor:
            cursor.execute(
                """UPDATE pipeline_runs
                   SET status=%s, row_counts=%s, error_message=%s,
                       finished_at=now()
                   WHERE run_id=%s""",
                (status, Jsonb(dict(row_counts)), error, run_id),
            )

    def table_counts(self) -> dict[str, int]:
        tables = list(CONFLICT_KEYS) + ["pipeline_runs"]
        output: dict[str, int] = {}
        with self.connection.cursor() as cursor:
            for table in tables:
                cursor.execute(f"SELECT count(*) AS count FROM {table}")
                output[table] = cursor.fetchone()["count"]
        return output
