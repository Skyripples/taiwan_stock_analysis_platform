"""Validate PostgreSQL dual-write counts, idempotency and JSON fidelity."""

from __future__ import annotations

import json
import logging
import random
import time
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from config import PROJECT_ROOT
from database.connection import configured, connect
from database.repository import StockRepository
from sync_stock_database import TABLES, build_batches, materialize_stock_ids, sync


LOGGER = logging.getLogger("database_sync_validation")
REPORT = PROJECT_ROOT / "data" / "analysis" / "database_sync_report.json"
FIXED = ["2330", "2317", "6488", "2881", "1101", "0050"]


def plain(value: Any) -> Any:
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral() else float(value)
    if isinstance(value, (datetime,)):
        return value.isoformat()
    return value


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def expected_counts(batches: dict[str, list[dict[str, Any]]]) -> dict[str, int]:
    return {table: len(rows) for table, rows in batches.items()}


def sample_symbols(batches: dict[str, list[dict[str, Any]]]) -> list[str]:
    available = sorted(row["symbol"] for row in batches["stocks"])
    chosen = random.Random(311).sample(available, min(50, len(available)))
    return list(dict.fromkeys(FIXED + chosen))


def compare_samples(connection, batches: dict[str, list[dict[str, Any]]], symbols: list[str]) -> dict[str, Any]:
    expected_stocks = {row["symbol"]: row for row in batches["stocks"] if row["symbol"] in symbols}
    failures: list[dict[str, Any]] = []
    checked = 0
    with connection.cursor() as cursor:
        cursor.execute(
            """SELECT symbol,name,market,industry,instrument_type,active,cached,cache_status
               FROM stocks WHERE symbol = ANY(%s)""", (symbols,)
        )
        actual = {row["symbol"]: row for row in cursor.fetchall()}
        for symbol, expected in expected_stocks.items():
            row = actual.get(symbol); checked += 1
            for key in ("name", "market", "industry", "instrument_type", "active", "cached", "cache_status"):
                if row is None or plain(row.get(key)) != expected.get(key):
                    failures.append({"symbol": symbol, "table": "stocks", "field": key,
                                     "json": expected.get(key), "database": plain(row.get(key)) if row else None})
        # source_payload is the lossless comparison anchor for nullable numeric records.
        for table, date_key in (("stock_quotes", "trade_date"), ("stock_valuations", "valuation_date"), ("stock_chips", "trade_date")):
            expected_rows = [row for row in batches[table] if row.get("symbol") in symbols]
            for item in expected_rows:
                cursor.execute(
                    f"""SELECT t.source_payload FROM {table} t JOIN stocks s USING(stock_id)
                         WHERE s.symbol=%s AND t.{date_key}=%s""",
                    (item["symbol"], item[date_key]),
                )
                found = cursor.fetchone(); checked += 1
                if not found or found["source_payload"] != item["source_payload"]:
                    failures.append({"symbol": item["symbol"], "table": table,
                                     "key": item[date_key], "reason": "source_payload mismatch"})
        for item in [row for row in batches["stock_monthly_revenue"] if row.get("symbol") in symbols]:
            cursor.execute(
                """SELECT t.source_payload FROM stock_monthly_revenue t JOIN stocks s USING(stock_id)
                   WHERE s.symbol=%s AND t.revenue_month=%s""",
                (item["symbol"], item["revenue_month"]),
            )
            found = cursor.fetchone(); checked += 1
            if not found or found["source_payload"] != item["source_payload"]:
                failures.append({"symbol": item["symbol"], "table": "stock_monthly_revenue",
                                 "key": item["revenue_month"], "reason": "source_payload mismatch"})
        for item in [row for row in batches["stock_financials"] if row.get("symbol") in symbols]:
            cursor.execute(
                """SELECT t.available_date, t.source_payload
                   FROM stock_financials t JOIN stocks s USING(stock_id)
                   WHERE s.symbol=%s AND t.fiscal_year=%s AND t.quarter=%s
                     AND t.statement_scope=%s""",
                (item["symbol"], item["fiscal_year"], item["quarter"], item["statement_scope"]),
            )
            found = cursor.fetchone(); checked += 1
            if (not found or found["source_payload"] != item["source_payload"]
                    or found["available_date"].isoformat() != item["available_date"]):
                failures.append({"symbol": item["symbol"], "table": "stock_financials",
                                 "key": f"{item['fiscal_year']}Q{item['quarter']}",
                                 "reason": "payload or available_date mismatch"})
    return {"symbols": symbols, "records_checked": checked, "failures": failures,
            "passed": not failures}


def transactional_tests(connection) -> dict[str, bool]:
    symbol = "2330"
    with connection.cursor() as cursor:
        cursor.execute("SELECT name FROM stocks WHERE symbol=%s", (symbol,))
        original = cursor.fetchone()["name"]
        cursor.execute(
            """INSERT INTO stocks(symbol,name,market,instrument_type)
               VALUES(%s,%s,'TWSE','company')
               ON CONFLICT(symbol) DO UPDATE SET name=EXCLUDED.name, updated_at=now()""",
            (symbol, "__UPSERT_TEST__"),
        )
        cursor.execute("SELECT name FROM stocks WHERE symbol=%s", (symbol,))
        update_visible = cursor.fetchone()["name"] == "__UPSERT_TEST__"
    connection.rollback()
    with connection.cursor() as cursor:
        cursor.execute("SELECT name FROM stocks WHERE symbol=%s", (symbol,))
        rollback_restored = cursor.fetchone()["name"] == original
        cursor.execute(
            """INSERT INTO stocks(symbol,name,market,instrument_type)
               VALUES('__ROLLBACK_TEST__','rollback','TEST','other')"""
        )
    connection.rollback()
    with connection.cursor() as cursor:
        cursor.execute("SELECT count(*) AS count FROM stocks WHERE symbol='__ROLLBACK_TEST__'")
        insert_rolled_back = cursor.fetchone()["count"] == 0
    connection.rollback()
    return {"upsert_update_path": update_visible, "rollback_restores_update": rollback_restored,
            "rollback_removes_insert": insert_rolled_back}


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(message)s")
    started = time.monotonic(); batches, updated_at = build_batches("full")
    report: dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "status": "not_run", "json_source_updated_at": updated_at,
        "json_expected_rows": expected_counts(batches), "database": None,
        "sample_validation": None, "idempotency": None, "transaction_tests": None,
        "limitations": [],
    }
    if not configured():
        report["limitations"].append("DB_* environment variables are not configured; live PostgreSQL validation was not run.")
        report["duration_seconds"] = round(time.monotonic() - started, 3)
        atomic_json(REPORT, report)
        LOGGER.warning("DB is not configured; wrote an explicit not_run report")
        return 0
    connection = connect()
    try:
        repository = StockRepository(connection)
        with connection.cursor() as cursor:
            cursor.execute("SHOW server_version")
            version = cursor.fetchone()["server_version"]
            cursor.execute("SELECT pg_database_size(current_database()) AS bytes")
            size = cursor.fetchone()["bytes"]
        connection.rollback()
        before = repository.table_counts(); connection.rollback()
        samples = compare_samples(connection, batches, sample_symbols(batches)); connection.rollback()
        tx_tests = transactional_tests(connection)
        rerun_batches, rerun_updated = build_batches("full")
        with connection.transaction():
            sync(connection, rerun_batches, "validation_rerun", rerun_updated)
        after = repository.table_counts(); connection.rollback()
        data_tables = [table for table in TABLES]
        stable = all(before[table] == after[table] for table in data_tables)
        report.update({
            "status": "passed" if samples["passed"] and stable and all(tx_tests.values()) else "failed",
            "database": {"postgresql_version": version, "size_bytes": size, "table_rows": after},
            "sample_validation": samples,
            "idempotency": {"before": before, "after": after, "data_table_counts_unchanged": stable},
            "transaction_tests": tx_tests,
        })
    except Exception as exc:
        report["status"] = "failed"; report["limitations"].append(str(exc))
        LOGGER.exception("Database validation failed")
    finally:
        connection.close()
    report["duration_seconds"] = round(time.monotonic() - started, 3)
    atomic_json(REPORT, report)
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
