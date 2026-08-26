"""Database, source and point-in-time validation for V3.13 global backfill."""

from __future__ import annotations

import json
import os
import random
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from backfill_global_market import Client, INSTRUMENTS, REPORT, fetch_fred, fetch_vix, fetch_yahoo
from config import PROJECT_ROOT
from database.connection import connect
from database.global_market_repository import GlobalMarketRepository


UTC = timezone.utc


def scalar(cursor, query: str, params: tuple[Any, ...] = ()) -> Any:
    cursor.execute(query, params)
    row = cursor.fetchone()
    return next(iter(row.values()))


def compare_samples(connection, kind: str, symbol: str, source_rows: list[dict[str, Any]]) -> dict[str, Any]:
    source = {str(row["trade_date" if kind == "daily" else "observation_date"]): row for row in source_rows}
    with connection.cursor() as cursor:
        if kind == "daily":
            cursor.execute("""SELECT p.trade_date::text AS day, p.close::float8 AS value
                              FROM market_daily_prices p JOIN market_instruments i USING(instrument_id)
                              WHERE i.canonical_symbol=%s ORDER BY p.trade_date""", (symbol,))
        else:
            cursor.execute("""SELECT o.observation_date::text AS day, o.value::float8 AS value
                              FROM macro_observations o JOIN macro_series s USING(macro_series_id)
                              WHERE s.series_key=%s ORDER BY o.observation_date""", (symbol,))
        database = cursor.fetchall()
    eligible = [row for row in database if row["day"] in source]
    sample = random.Random(f"V3.13:{symbol}").sample(eligible, min(5, len(eligible)))
    details, passed = [], 0
    for row in sample:
        expected = source[row["day"]]["close" if kind == "daily" else "value"]
        match = abs(float(row["value"]) - float(expected)) <= 1e-8
        passed += int(match)
        details.append({"date": row["day"], "database": row["value"], "source": expected, "match": match})
    return {"sample_count": len(sample), "passed": passed, "details": details}


def main() -> int:
    connection = connect()
    repository = GlobalMarketRepository(connection)
    client = Client()
    report = json.loads(REPORT.read_text(encoding="utf-8")) if REPORT.exists() else {}
    validation: dict[str, Any] = {"validated_at": datetime.now(UTC).isoformat(), "checks": {}, "source_samples": {}, "failures": []}
    try:
        with connection.cursor() as cursor:
            duplicate_queries = {
                "market_daily_prices": "SELECT count(*) FROM (SELECT instrument_id,trade_date FROM market_daily_prices GROUP BY 1,2 HAVING count(*)>1) q",
                "market_intraday_prices": "SELECT count(*) FROM (SELECT instrument_id,timestamp_utc,interval_seconds FROM market_intraday_prices GROUP BY 1,2,3 HAVING count(*)>1) q",
                "macro_observations": "SELECT count(*) FROM (SELECT macro_series_id,observation_date,available_at FROM macro_observations GROUP BY 1,2,3 HAVING count(*)>1) q",
                "market_features": "SELECT count(*) FROM (SELECT feature_key,target_date,target_scope,transform_version FROM market_features GROUP BY 1,2,3,4 HAVING count(*)>1) q",
            }
            duplicates = {name: scalar(cursor, query) for name, query in duplicate_queries.items()}
            validation["checks"]["duplicate_primary_keys"] = duplicates
            invalid_ohlc = scalar(cursor, """SELECT count(*) FROM (
                SELECT open,high,low,close FROM market_daily_prices
                UNION ALL SELECT open,high,low,close FROM market_intraday_prices
            ) q WHERE high IS NOT NULL AND low IS NOT NULL AND
              (high < low OR (open IS NOT NULL AND (open > high OR open < low)) OR
               (close IS NOT NULL AND (close > high OR close < low)))""")
            validation["checks"]["invalid_ohlc_rows"] = invalid_ohlc
            leakage = scalar(cursor, "SELECT count(*) FROM market_features WHERE available_at >= cutoff_at")
            validation["checks"]["feature_leakage_rows"] = leakage
            naive_or_missing = scalar(cursor, """SELECT
                (SELECT count(*) FROM market_daily_prices WHERE available_at IS NULL OR timezone IS NULL) +
                (SELECT count(*) FROM market_intraday_prices WHERE available_at IS NULL OR timezone IS NULL) +
                (SELECT count(*) FROM macro_observations WHERE available_at IS NULL)""")
            validation["checks"]["missing_availability_or_timezone"] = naive_or_missing
            cursor.execute("SELECT series_key, unit FROM macro_series ORDER BY series_key")
            units = {row["series_key"]: row["unit"] for row in cursor.fetchall()}
            validation["checks"]["macro_units"] = units
            validation["checks"]["rates_units_valid"] = units.get("DGS5") == "Percent" and units.get("DGS30") == "Percent" and units.get("T10Y3M") == "Percentage points" and units.get("ICSA") == "Number"
            cursor.execute("""SELECT i.canonical_symbol, count(*) AS rows, min(timestamp_utc) AS first_timestamp,
                                      max(timestamp_utc) AS last_timestamp, max(available_at-timestamp_utc) AS max_delay
                               FROM market_intraday_prices p JOIN market_instruments i USING(instrument_id)
                               GROUP BY i.canonical_symbol ORDER BY i.canonical_symbol""")
            validation["checks"]["crypto_ranges"] = [{k: (v.isoformat() if hasattr(v, "isoformat") else str(v)) for k, v in row.items()} for row in cursor.fetchall()]

        vix_source = fetch_vix(client, __import__("datetime").date(1990, 1, 2), __import__("datetime").date.today())
        validation["source_samples"]["VIX"] = compare_samples(connection, "daily", "VIX", vix_source)
        instrument_map = {item.symbol: item for item in INSTRUMENTS}
        source_start = __import__("datetime").date.today().replace(year=__import__("datetime").date.today().year-20)
        for symbol in ("NDX", "EWT", "COPPER", "WTI", "GOLD", "USDJPY", "USDKRW"):
            rows = fetch_yahoo(client, instrument_map[symbol], source_start, __import__("datetime").date.today())
            validation["source_samples"][symbol] = compare_samples(connection, "daily", symbol, rows)
        for series in ("DGS5", "DGS30", "T10Y3M", "ICSA"):
            rows = fetch_fred(client, series, __import__("datetime").date.today().replace(year=__import__("datetime").date.today().year-20), __import__("datetime").date.today())
            validation["source_samples"][series] = compare_samples(connection, "macro", series, rows)

        before = repository.table_counts()
        with connection.cursor() as cursor:
            cursor.execute("SELECT * FROM market_daily_prices ORDER BY instrument_id,trade_date LIMIT 1")
            sample_row = cursor.fetchone()
        if sample_row:
            allowed = ("instrument_id","trade_date","open","high","low","close","adjusted_close","volume","currency","session","timezone","available_at","source_updated_at","quality_flags","metadata")
            repository.upsert_many("market_daily_prices", [{key: sample_row[key] for key in allowed}])
            connection.commit()
        after = repository.table_counts()
        validation["checks"]["upsert_rerun_counts_unchanged"] = before == after

        rollback_key = "__rollback_validation__"
        try:
            with connection.transaction():
                repository.upsert_many("market_features", [{"feature_key": rollback_key, "target_date": "2099-01-01", "target_scope": "test",
                    "cutoff_at": "2099-01-01T09:00:00+08:00", "value": 1, "available_at": "2099-01-01T08:00:00+08:00",
                    "source_instrument_id": None, "transform_version": "test", "quality_flags": {}, "metadata": {}}])
                raise RuntimeError("intentional rollback")
        except RuntimeError:
            pass
        with connection.cursor() as cursor:
            validation["checks"]["transaction_rollback"] = scalar(cursor, "SELECT count(*) FROM market_features WHERE feature_key=%s", (rollback_key,)) == 0

        required_zero = [duplicates == {key: 0 for key in duplicates}, invalid_ohlc == 0, leakage == 0, naive_or_missing == 0,
                         validation["checks"]["rates_units_valid"], validation["checks"]["upsert_rerun_counts_unchanged"], validation["checks"]["transaction_rollback"]]
        samples_ok = all(item["sample_count"] == 5 and item["passed"] == 5 for item in validation["source_samples"].values())
        validation["status"] = "passed" if all(required_zero) and samples_ok else "failed"
        if validation["status"] != "passed":
            validation["failures"].append("One or more database/source checks failed")
        report["validation"] = validation
        report["table_counts"] = repository.table_counts()
        temporary = REPORT.with_name(f".{REPORT.name}.{os.getpid()}.tmp")
        temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
        os.replace(temporary, REPORT)
        print(json.dumps({"status": validation["status"], "checks": validation["checks"], "samples": {k: v["passed"] for k,v in validation["source_samples"].items()}}, ensure_ascii=False, default=str))
        return 0 if validation["status"] == "passed" else 1
    finally:
        connection.close()


if __name__ == "__main__":
    raise SystemExit(main())
