"""PostgreSQL repository for normalized global market datasets."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any


CONFLICT_KEYS = {
    "data_sources": ("source_key",),
    "market_instruments": ("canonical_symbol",),
    "market_daily_prices": ("instrument_id", "trade_date"),
    "market_intraday_prices": ("instrument_id", "timestamp_utc", "interval_seconds"),
    "macro_series": ("series_key",),
    "macro_observations": ("macro_series_id", "observation_date", "available_at"),
    "market_features": ("feature_key", "target_date", "target_scope", "transform_version"),
    "global_backfill_checkpoints": ("task_key",),
}
JSON_COLUMNS = {"metadata", "quality_flags"}


class GlobalMarketRepository:
    def __init__(self, connection):
        self.connection = connection

    @staticmethod
    def _values(row: Mapping[str, Any]) -> dict[str, Any]:
        from psycopg.types.json import Jsonb

        return {
            key: Jsonb(value) if key in JSON_COLUMNS and value is not None else value
            for key, value in row.items()
        }

    def upsert_many(self, table: str, rows: Iterable[Mapping[str, Any]]) -> int:
        if table not in CONFLICT_KEYS:
            raise ValueError(f"Unsupported global market table: {table}")
        materialized = [self._values(row) for row in rows]
        if not materialized:
            return 0
        columns = tuple(materialized[0])
        if any(tuple(row) != columns for row in materialized):
            raise ValueError(f"Rows for {table} do not share columns")
        from psycopg import sql

        conflict = CONFLICT_KEYS[table]
        updates = [c for c in columns if c not in conflict and c != "created_at"]
        assignments = [
            sql.SQL("{}=EXCLUDED.{}").format(sql.Identifier(c), sql.Identifier(c))
            for c in updates
        ]
        assignments.append(sql.SQL("updated_at=now()"))
        statement = sql.SQL(
            "INSERT INTO {table} ({columns}) VALUES ({values}) "
            "ON CONFLICT ({conflict}) DO UPDATE SET {updates}"
        ).format(
            table=sql.Identifier(table),
            columns=sql.SQL(",").join(map(sql.Identifier, columns)),
            values=sql.SQL(",").join(sql.Placeholder(c) for c in columns),
            conflict=sql.SQL(",").join(map(sql.Identifier, conflict)),
            updates=sql.SQL(",").join(assignments),
        )
        with self.connection.cursor() as cursor:
            cursor.executemany(statement, materialized)
        return len(materialized)

    def source_ids(self) -> dict[str, int]:
        with self.connection.cursor() as cursor:
            cursor.execute("SELECT source_key, source_id FROM data_sources")
            return {row["source_key"]: row["source_id"] for row in cursor.fetchall()}

    def instrument_ids(self) -> dict[str, int]:
        with self.connection.cursor() as cursor:
            cursor.execute("SELECT canonical_symbol, instrument_id FROM market_instruments")
            return {row["canonical_symbol"]: row["instrument_id"] for row in cursor.fetchall()}

    def series_ids(self) -> dict[str, int]:
        with self.connection.cursor() as cursor:
            cursor.execute("SELECT series_key, macro_series_id FROM macro_series")
            return {row["series_key"]: row["macro_series_id"] for row in cursor.fetchall()}

    def checkpoint(self, task_key: str) -> dict[str, Any] | None:
        with self.connection.cursor() as cursor:
            cursor.execute(
                "SELECT * FROM global_backfill_checkpoints WHERE task_key=%s", (task_key,)
            )
            return cursor.fetchone()

    def save_checkpoint(
        self, task_key: str, status: str, cursor_value: str | None,
        rows_written: int, error: str | None = None, metadata: Mapping[str, Any] | None = None,
    ) -> None:
        self.upsert_many("global_backfill_checkpoints", [{
            "task_key": task_key, "status": status, "last_cursor": cursor_value,
            "rows_written": rows_written, "started_at": None,
            "finished_at": None, "error_message": error, "metadata": dict(metadata or {}),
        }])

    def table_counts(self) -> dict[str, int]:
        tables = (
            "data_sources", "market_instruments", "market_daily_prices",
            "market_intraday_prices", "macro_series", "macro_observations",
            "market_features", "global_backfill_checkpoints",
        )
        output: dict[str, int] = {}
        with self.connection.cursor() as cursor:
            for table in tables:
                cursor.execute(f"SELECT count(*) AS count FROM {table}")
                output[table] = cursor.fetchone()["count"]
        return output
