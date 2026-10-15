"""Import staged TAIFEX daily futures rows into PostgreSQL."""
from __future__ import annotations

import argparse
import gzip
import json
from datetime import date
from typing import Iterable

from config import PROJECT_ROOT
from database.connection import connect

STAGING = PROJECT_ROOT / "data_lake" / "staging" / "futures_daily"
MIGRATION = PROJECT_ROOT / "scripts" / "database" / "migrations" / "008_futures_market_foundation.sql"
PRODUCTS = ("TX", "MTX", "TMF")
LISTING_DATES = {"TMF": date(2024, 7, 29)}
FIELDS = (
    "trade_date", "product", "contract", "session", "open", "high", "low", "close",
    "settlement_price", "volume", "open_interest", "expiry_date", "days_to_expiry",
    "contract_rank", "roll_date", "is_expiry_week", "spot_index", "basis", "basis_pct",
    "source", "source_date", "available_at", "fetched_at",
)


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbols", nargs="+", choices=PRODUCTS, default=list(PRODUCTS))
    parser.add_argument("--start")
    parser.add_argument("--end")
    parser.add_argument("--batch-size", type=int, default=2000)
    parser.add_argument("--init-schema", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--verify", action="store_true")
    return parser.parse_args()


def iter_rows(symbols: set[str], start: date | None, end: date | None) -> Iterable[dict]:
    for path in sorted(STAGING.glob("product=*/year=*/daily.jsonl.gz")):
        with gzip.open(path, "rt", encoding="utf-8") as stream:
            for line in stream:
                if not line.strip():
                    continue
                row = json.loads(line)
                day = date.fromisoformat(row["trade_date"])
                if row["product"] not in symbols or (start and day < start) or (end and day > end):
                    continue
                if day < LISTING_DATES.get(row["product"], date.min):
                    raise ValueError(f"pre-listing row: {row['product']} {day}")
                yield row


def normalized(row: dict) -> tuple:
    return tuple(row.get(name) for name in FIELDS)


def verify(connection, symbols: list[str]) -> list[dict]:
    with connection.cursor() as cursor:
        cursor.execute(
            """SELECT product, count(*) AS rows, min(trade_date) AS first_date,
                      max(trade_date) AS last_date, count(DISTINCT contract) AS contracts
                 FROM futures_daily_prices WHERE product = ANY(%s)
                GROUP BY product ORDER BY product""",
            (symbols,),
        )
        result = cursor.fetchall()
        cursor.execute(
            """SELECT count(*) - count(DISTINCT (trade_date, product, contract, session)) AS duplicates
                 FROM futures_daily_prices WHERE product = ANY(%s)""",
            (symbols,),
        )
        duplicate_count = cursor.fetchone()["duplicates"]
    return [{**dict(row), "duplicate_pk": duplicate_count} for row in result]


def main() -> int:
    options = arguments()
    if options.batch_size < 1:
        raise ValueError("batch-size must be positive")
    start = date.fromisoformat(options.start) if options.start else None
    end = date.fromisoformat(options.end) if options.end else None
    rows = list(iter_rows(set(options.symbols), start, end))
    keys = [(r["trade_date"], r["product"], r["contract"], r["session"]) for r in rows]
    if len(keys) != len(set(keys)):
        raise ValueError("duplicate staged futures primary key")
    if any(r["trade_date"] == "2026-07-10" for r in rows):
        raise ValueError("persistent official-empty date must not be imported")
    if options.dry_run:
        print(json.dumps({"dry_run": True, "rows": len(rows), "duplicates": 0, "symbols": options.symbols}))
        return 0

    connection = connect()
    try:
        if options.init_schema:
            with connection.transaction(), connection.cursor() as cursor:
                cursor.execute(MIGRATION.read_text(encoding="utf-8"))
                cursor.execute(
                    """INSERT INTO schema_migrations(version) VALUES (%s)
                       ON CONFLICT (version) DO NOTHING""",
                    (MIGRATION.name,),
                )
        if not options.verify:
            placeholders = ",".join(["%s"] * len(FIELDS))
            updates = ",".join(
                f"{name}=EXCLUDED.{name}" for name in FIELDS
                if name not in {"trade_date", "product", "contract", "session"}
            ) + ",updated_at=now()"
            statement = (
                f"INSERT INTO futures_daily_prices ({','.join(FIELDS)}) VALUES ({placeholders}) "
                f"ON CONFLICT (trade_date,product,contract,session) DO UPDATE SET {updates}"
            )
            for offset in range(0, len(rows), options.batch_size):
                with connection.transaction(), connection.cursor() as cursor:
                    cursor.executemany(statement, [normalized(row) for row in rows[offset:offset + options.batch_size]])
        print(json.dumps({"processed_rows": 0 if options.verify else len(rows), "verification": verify(connection, options.symbols)}, default=str))
        return 0
    finally:
        connection.close()


if __name__ == "__main__":
    raise SystemExit(main())
