"""Bulk, idempotent import of canonical stock-history JSONL into PostgreSQL."""

from __future__ import annotations

import argparse
import gzip
import json
import time
from pathlib import Path
from typing import Any, Iterable, Iterator

from config import PROJECT_ROOT
from database.connection import connect


ROOT = PROJECT_ROOT / "data_lake" / "staging" / "stock_history"

# Keep the public tuple contract stable: table, date field, dataset fields.
SPECS = {
    "revenue": ("stock_monthly_revenue", "revenue_month", ("revenue", "revenue_yoy", "revenue_mom")),
    "financials": (
        "stock_financials",
        "period_end",
        (
            "fiscal_year", "quarter", "statement_scope", "period_end", "published_date",
            "available_date", "available_at", "eps", "revenue", "gross_profit",
            "operating_income", "net_income", "gross_margin", "operating_margin",
            "net_margin", "roe", "roa", "total_assets", "total_liabilities", "equity",
            "debt_ratio", "current_ratio", "operating_cash_flow", "investing_cash_flow",
            "financing_cash_flow", "capital_expenditure", "free_cash_flow",
        ),
    ),
    "valuation": ("stock_valuations", "valuation_date", ("valuation_date", "pe", "pb", "dividend_yield")),
    "chips": (
        "stock_chips",
        "trade_date",
        (
            "trade_date", "foreign_net", "investment_trust_net", "dealer_net",
            "institutional_total", "margin_balance", "margin_change", "short_balance",
            "short_change",
        ),
    ),
}

CONFLICT_FIELDS = {
    "revenue": ("stock_id", "revenue_month"),
    "financials": ("stock_id", "fiscal_year", "quarter", "statement_scope"),
    "valuation": ("stock_id", "valuation_date"),
    "chips": ("stock_id", "trade_date"),
}

FIELD_TYPES = {
    "symbol": "text",
    "revenue_month": "date",
    "valuation_date": "date",
    "trade_date": "date",
    "fiscal_year": "integer",
    "quarter": "smallint",
    "statement_scope": "text",
    "period_end": "date",
    "published_date": "date",
    "available_date": "date",
    "available_at": "timestamptz",
    "source": "text",
    "source_date": "date",
    "fetched_at": "timestamptz",
    "source_payload": "jsonb",
}

PRECEDENCE_SQL = (
    "(EXCLUDED.source_date IS NOT NULL AND "
    "({table}.source_date IS NULL OR EXCLUDED.source_date >= {table}.source_date)) "
    "OR (EXCLUDED.source_date IS NULL AND {table}.source_date IS NULL AND "
    "({table}.fetched_at IS NULL OR EXCLUDED.fetched_at >= {table}.fetched_at))"
)


def args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", choices=(*SPECS, "all"), default="all")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--batch-size", type=int, default=1000)
    return parser.parse_args()


def rows(dataset: str) -> Iterator[dict[str, Any]]:
    """Stream canonical rows without materializing a dataset in memory."""
    for path in sorted((ROOT / dataset).glob("*.jsonl.gz")):
        with gzip.open(path, "rt", encoding="utf-8") as stream:
            for line in stream:
                if line.strip():
                    yield json.loads(line)


def batches(items: Iterable[Any], size: int) -> Iterator[list[Any]]:
    batch: list[Any] = []
    for item in items:
        batch.append(item)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch


def dataset_fields(dataset: str) -> tuple[str, ...]:
    _, _, fields = SPECS[dataset]
    return (("revenue_month", *fields) if dataset == "revenue" else fields)


def target_columns(dataset: str) -> tuple[str, ...]:
    return ("stock_id", *dataset_fields(dataset), "source", "source_date", "fetched_at", "source_payload")


def staging_columns(dataset: str) -> tuple[str, ...]:
    return ("_sequence", "symbol", *dataset_fields(dataset), "source", "source_date", "fetched_at", "source_payload")


def field_type(field: str) -> str:
    return FIELD_TYPES.get(field, "numeric")


def staging_table(dataset: str) -> str:
    return f"_stock_history_import_{dataset}"


def create_staging_sql(dataset: str) -> str:
    definitions = ["_sequence bigint NOT NULL", "symbol text NOT NULL"]
    definitions.extend(f"{field} {field_type(field)}" for field in dataset_fields(dataset))
    definitions.extend(("source text", "source_date date", "fetched_at timestamptz", "source_payload jsonb NOT NULL"))
    return f"CREATE TEMP TABLE IF NOT EXISTS {staging_table(dataset)} ({','.join(definitions)}) ON COMMIT PRESERVE ROWS"


def copy_sql(dataset: str) -> str:
    return f"COPY {staging_table(dataset)} ({','.join(staging_columns(dataset))}) FROM STDIN"


def merge_sql(dataset: str) -> str:
    table, date_field, _ = SPECS[dataset]
    stage = staging_table(dataset)
    columns = target_columns(dataset)
    conflict = CONFLICT_FIELDS[dataset]
    key_without_stock = tuple(field for field in conflict if field != "stock_id")
    updates = tuple(
        field for field in columns
        if field not in {"stock_id", "fiscal_year", "quarter", "statement_scope", date_field}
    )
    selected = ",".join(f"st.{field}" for field in columns if field != "stock_id")
    matched_columns = ",".join(columns)
    conflict_sql = ",".join(conflict)
    ordering = ",".join((
        "stock_id", *key_without_stock, "source_date DESC NULLS LAST",
        "fetched_at DESC NULLS LAST", "_sequence DESC",
    ))
    update_sql = ",".join(f"{field}=EXCLUDED.{field}" for field in updates)
    precedence = PRECEDENCE_SQL.format(table=table)
    return f"""
WITH matched_raw AS MATERIALIZED (
    SELECT s.stock_id,st._sequence,{selected}
    FROM {stage} st JOIN stocks s ON s.symbol=st.symbol
), matched AS MATERIALIZED (
    SELECT DISTINCT ON ({conflict_sql}) {matched_columns}
    FROM matched_raw ORDER BY {ordering}
), upserted AS (
    INSERT INTO {table} ({matched_columns})
    SELECT {matched_columns} FROM matched
    ON CONFLICT ({conflict_sql}) DO UPDATE SET {update_sql}
    WHERE {precedence}
    RETURNING 1
), unmatched AS (
    SELECT coalesce(array_agg(DISTINCT st.symbol ORDER BY st.symbol),ARRAY[]::text[]) AS symbols
    FROM {stage} st LEFT JOIN stocks s ON s.symbol=st.symbol WHERE s.stock_id IS NULL
)
SELECT (SELECT count(*) FROM matched_raw) AS matched_stocks,
       (SELECT count(*) FROM upserted) AS inserted_or_updated,
       (SELECT symbols FROM unmatched) AS unmatched_symbols
""".strip()


def copy_values(dataset: str, item: dict[str, Any], sequence: int, json_adapter: Any) -> tuple[Any, ...]:
    return (
        sequence,
        item["symbol"],
        *(item.get(field) for field in dataset_fields(dataset)),
        item.get("source"),
        item.get("source_date"),
        item.get("fetched_at"),
        json_adapter(item),
    )


def result_values(row: Any) -> tuple[int, int, list[str]]:
    if isinstance(row, dict):
        return int(row["matched_stocks"]), int(row["inserted_or_updated"]), list(row["unmatched_symbols"] or [])
    return int(row[0]), int(row[1]), list(row[2] or [])


def precedence_allows(
    target_source_date: Any,
    target_fetched_at: Any,
    incoming_source_date: Any,
    incoming_fetched_at: Any,
) -> bool:
    """Python truth-table equivalent of the UPSERT freshness predicate."""
    if incoming_source_date is not None:
        return target_source_date is None or incoming_source_date >= target_source_date
    if target_source_date is not None:
        return False
    return target_fetched_at is None or (
        incoming_fetched_at is not None and incoming_fetched_at >= target_fetched_at
    )


def import_dataset(connection: Any, dataset: str, batch_size: int) -> dict[str, Any]:
    """COPY chunks into a temp table, then resolve symbols and UPSERT once per chunk."""
    from psycopg.types.json import Jsonb

    started = time.monotonic()
    processed = matched = affected = 0
    unknown: set[str] = set()

    with connection.transaction(), connection.cursor() as cursor:
        cursor.execute(create_staging_sql(dataset))

    for chunk in batches(rows(dataset), batch_size):
        with connection.transaction(), connection.cursor() as cursor:
            cursor.execute(f"TRUNCATE {staging_table(dataset)}")
            with cursor.copy(copy_sql(dataset)) as copy:
                for sequence, item in enumerate(chunk):
                    copy.write_row(copy_values(dataset, item, sequence, Jsonb))
            cursor.execute(merge_sql(dataset))
            batch_matched, batch_affected, batch_unknown = result_values(cursor.fetchone())
        processed += len(chunk)
        matched += batch_matched
        affected += batch_affected
        unknown.update(batch_unknown)

    return {
        "dataset": dataset,
        "processed": processed,
        "matched_stocks": matched,
        "unmatched_symbols": len(unknown),
        "unmatched_symbol_values": sorted(unknown),
        "inserted_or_updated": affected,
        "elapsed_seconds": round(time.monotonic() - started, 3),
    }


def main() -> int:
    options = args()
    selected = tuple(SPECS) if options.dataset == "all" else (options.dataset,)
    if options.batch_size < 1:
        raise ValueError("batch-size must be positive")
    if options.dry_run:
        counts = {name: sum(1 for _ in rows(name)) for name in selected}
        print(json.dumps({"dry_run": True, "rows": counts}, ensure_ascii=False))
        return 0

    connection = connect()
    try:
        statistics = [import_dataset(connection, dataset, options.batch_size) for dataset in selected]
    finally:
        connection.close()

    result: dict[str, Any] = {
        "dry_run": False,
        "rows": {item["dataset"]: item["processed"] for item in statistics},
        "datasets": statistics,
    }
    if len(statistics) == 1:
        result.update(statistics[0])
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
