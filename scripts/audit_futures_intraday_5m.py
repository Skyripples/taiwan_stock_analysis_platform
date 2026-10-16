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
        print(json.dumps({"files": 0, "rows": 0, "status": "empty"})); return 0
    import duckdb
    db = duckdb.connect()
    try:
        row = db.execute("""SELECT count(*) rows,count(DISTINCT trading_date) trading_dates,
          min(timestamp_utc) min_timestamp,max(timestamp_utc) max_timestamp,
          count(DISTINCT contract) contracts,
          count(*)-count(DISTINCT (product,contract,session,timestamp_utc,interval_seconds)) duplicate_pk,
          count(*) FILTER(WHERE high<greatest(open,close) OR low>least(open,close)) invalid_ohlc,
          count(*) FILTER(WHERE volume<0) negative_volume,
          count(*) FILTER(WHERE epoch(timestamp_utc)::BIGINT%300<>0) misaligned
          FROM read_parquet(?,hive_partitioning=true,union_by_name=true)""", [pattern]).fetchone()
        products = db.execute("""SELECT product,count(*) rows,
          count(*) FILTER(WHERE session='regular') regular,count(*) FILTER(WHERE session='after_hours') after_hours
          FROM read_parquet(?,hive_partitioning=true,union_by_name=true) GROUP BY product ORDER BY product""", [pattern]).fetchall()
    finally: db.close()
    result = {"files": len(files), "rows": row[0], "trading_dates": row[1], "min_timestamp": str(row[2]),
              "max_timestamp": str(row[3]), "contracts": row[4], "duplicate_pk": row[5],
              "invalid_ohlc": row[6], "negative_volume": row[7], "misaligned": row[8],
              "products": [{"product": item[0], "rows": item[1], "regular": item[2], "after_hours": item[3]} for item in products]}
    print(json.dumps(result, ensure_ascii=False, indent=2)); return 1 if any(result[key] for key in ("duplicate_pk", "invalid_ohlc", "negative_volume", "misaligned")) else 0


if __name__ == "__main__": raise SystemExit(main())
