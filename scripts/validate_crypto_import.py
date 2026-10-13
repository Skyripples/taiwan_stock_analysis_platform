"""Validate production Coinbase 5-minute rows without exposing credentials."""

from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal

from database.connection import connect


def serial(value):
    if isinstance(value, datetime): return value.isoformat()
    if isinstance(value, Decimal): return float(value)
    raise TypeError(type(value).__name__)


def main() -> int:
    connection=connect()
    try:
        with connection.cursor() as cursor:
            cursor.execute("SET TIME ZONE 'UTC'")
            cursor.execute("""WITH scoped AS (
                SELECT mi.source_symbol AS symbol,p.*,ds.source_key
                FROM market_intraday_prices p JOIN market_instruments mi USING(instrument_id)
                JOIN data_sources ds ON ds.source_id=mi.source_id
                WHERE mi.source_symbol=ANY(%s) AND p.interval_seconds=300
              ), aggregates AS (
                SELECT symbol,count(*) AS rows,min(timestamp_utc) AS first_timestamp,max(timestamp_utc) AS last_timestamp,
                  ((extract(epoch FROM max(timestamp_utc)-min(timestamp_utc))/300)::bigint+1)-count(*) AS missing_candles,
                  count(*) FILTER (WHERE open IS NULL OR high IS NULL OR low IS NULL OR close IS NULL OR volume IS NULL) AS null_ohlcv,
                  count(*) FILTER (WHERE high<greatest(open,close,low) OR low>least(open,close,high) OR volume<0) AS malformed_ohlcv,
                  count(*) FILTER (WHERE mod(extract(epoch FROM timestamp_utc)::bigint,300)<>0) AS misaligned_timestamp,
                  count(*) FILTER (WHERE timezone<>'UTC') AS non_utc_timezone,
                  count(*) FILTER (WHERE source_key<>'coinbase' OR metadata->>'source_key'<>'coinbase') AS wrong_source
                FROM scoped GROUP BY symbol
              ), duplicates AS (
                SELECT coalesce(sum(extra),0) AS duplicate_count FROM (
                  SELECT count(*)-1 AS extra FROM scoped GROUP BY instrument_id,timestamp_utc,interval_seconds HAVING count(*)>1
                ) d
              ) SELECT a.*,d.duplicate_count FROM aggregates a CROSS JOIN duplicates d ORDER BY symbol""", (["BTC-USD","ETH-USD"],))
            rows=cursor.fetchall()
        print(json.dumps({"source":"coinbase","interval_seconds":300,"symbols":rows},default=serial,ensure_ascii=False,indent=2))
        return 0 if rows and all(not row["duplicate_count"] and not row["null_ohlcv"] and not row["malformed_ohlcv"] and not row["misaligned_timestamp"] and not row["non_utc_timezone"] and not row["wrong_source"] for row in rows) else 1
    finally:
        connection.close()


if __name__=="__main__": raise SystemExit(main())
