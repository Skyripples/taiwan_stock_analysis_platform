"""Refresh PIT sessions from locally cached, official-derived trading observations."""
from __future__ import annotations

import argparse
import csv
import sys
from datetime import date
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
PROJECT_ROOT = SCRIPTS.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from research.backfill_taiwan_pit_foundation import build_sessions, write_partitioned


def local_official_dates(start: date, end: date) -> list[date]:
    import duckdb
    price_glob = str(PROJECT_ROOT / "data_lake/stock_daily_prices/**/*.parquet").replace("\\", "/")
    connection = duckdb.connect()
    try:
        values = {row[0] for row in connection.execute(
            "SELECT DISTINCT trade_date FROM read_parquet(?, hive_partitioning=true) WHERE trade_date BETWEEN ? AND ?",
            [price_glob, start, end],
        ).fetchall()}
    finally:
        connection.close()
    for relative in ("data/history/backfill_market_daily.csv", "data/history/market_daily.csv"):
        with (PROJECT_ROOT / relative).open("r", encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                value = date.fromisoformat(row["trade_date"])
                if start <= value <= end and row.get("taiwan_market_trade_date") == row["trade_date"]:
                    values.add(value)
    return sorted(values)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", required=True, type=date.fromisoformat)
    parser.add_argument("--end", required=True, type=date.fromisoformat)
    args = parser.parse_args()
    dates = local_official_dates(args.start, args.end)
    weekend_dates = [value for value in dates if value.weekday() >= 5]
    if weekend_dates:
        raise ValueError(f"Official-derived observations contain non-session weekend dates: {weekend_dates}")
    write_partitioned(build_sessions(dates, args.start, args.end), "market_sessions", "calendar_date", ("market", "calendar_date"))
    print(f"PIT calendar refreshed | sessions={len(dates)} range={args.start}..{args.end}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
