"""Offline quality audit for staged TAIFEX reconstructed 5-minute candles."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from config import PROJECT_ROOT

DEFAULT_ROOT = PROJECT_ROOT / "data_lake/staging/futures_intraday_5m"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__); parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    options = parser.parse_args(); pattern = str(options.root / "trading_date=*/product=*/candles.parquet").replace("\\", "/")
    files = list(options.root.glob("trading_date=*/product=*/candles.parquet"))
    if not files:
        print(json.dumps({"files": 0, "row_count": 0, "status": "empty"})); return 0
    import duckdb
    db = duckdb.connect()
    try:
        row = db.execute("""SELECT count(*) AS row_count,count(DISTINCT trading_date) AS trading_dates,
          min(timestamp_utc) AS min_timestamp,max(timestamp_utc) AS max_timestamp,
          count(DISTINCT contract) AS contracts,
          count(*)-count(DISTINCT (product,contract,session,timestamp_utc,interval_seconds)) AS duplicates,
          count(*) FILTER(WHERE high<greatest(open,close) OR low>least(open,close)) AS invalid_ohlc,
          count(*) FILTER(WHERE volume<0) AS invalid_volume,
          count(*) FILTER(WHERE epoch(timestamp_utc)::BIGINT%300<>0) AS misaligned
          FROM read_parquet(?,hive_partitioning=true,union_by_name=true)""", [pattern]).fetchone()
        products = db.execute("""SELECT product,count(*) AS row_count,
          count(*) FILTER(WHERE session='regular') AS regular,count(*) FILTER(WHERE session='after_hours') AS after_hours
          FROM read_parquet(?,hive_partitioning=true,union_by_name=true) GROUP BY product ORDER BY product""", [pattern]).fetchall()
    finally: db.close()
    result = {"files": len(files), "row_count": row[0], "trading_dates": row[1], "min_timestamp": str(row[2]),
              "max_timestamp": str(row[3]), "contracts": row[4], "duplicates": row[5],
              "invalid_ohlc": row[6], "invalid_volume": row[7], "misaligned": row[8],
              "products": [{"product": item[0], "row_count": item[1], "regular": item[2], "after_hours": item[3]} for item in products]}
    print(json.dumps(result, ensure_ascii=False, indent=2)); return 1 if any(result[key] for key in ("duplicates", "invalid_ohlc", "invalid_volume", "misaligned")) else 0


if __name__ == "__main__": raise SystemExit(main())
