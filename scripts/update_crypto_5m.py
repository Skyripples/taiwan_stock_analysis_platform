"""Incrementally upsert completed Coinbase 5-minute candles into PostgreSQL."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone

from crypto_market import FIVE_MINUTES, PRODUCTS, completed_candle_end, completed_rows, missing_candle_windows, utc_datetime
from database.connection import connect
from database.global_market_repository import GlobalMarketRepository
from research.backfill_global_market import Client, fetch_crypto_chunk

UTC = timezone.utc


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Update completed Coinbase 5-minute candles")
    parser.add_argument("--symbols", nargs="+", choices=tuple(PRODUCTS), default=list(PRODUCTS))
    parser.add_argument("--lookback-hours", type=int, default=48)
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def update(options: argparse.Namespace, now: datetime | None = None, client=None, connection=None) -> dict:
    end = completed_candle_end(now)
    own_connection = connection is None
    connection = connection or connect()
    repository = GlobalMarketRepository(connection)
    result = {"status": "complete", "completed_candle_end": end.isoformat(), "symbols": {}, "failures": []}
    try:
        instrument_ids = repository.instrument_ids()
        client = client or Client()
        for product in options.symbols:
            canonical = PRODUCTS[product]
            try:
                instrument_id = instrument_ids[canonical]
                start = end - timedelta(hours=max(1, options.lookback_hours))
                with connection.cursor() as cursor:
                    cursor.execute("""SELECT timestamp_utc FROM market_intraday_prices
                                      WHERE instrument_id=%s AND interval_seconds=%s AND timestamp_utc >= %s AND timestamp_utc < %s""",
                                   (instrument_id, FIVE_MINUTES, start, end))
                    existing = {utc_datetime(row["timestamp_utc"]) for row in cursor.fetchall()}
                windows = missing_candle_windows(start, end, existing)
                if options.dry_run:
                    result["symbols"][product] = {"missing_candles": sum(int((right-left).total_seconds()//FIVE_MINUTES) for left,right in windows), "requests": len(windows), "upserted": 0}
                    continue
                rows = []
                for left, right in windows:
                    rows.extend(fetch_crypto_chunk(client, product, left, right, FIVE_MINUTES))
                rows = completed_rows(rows, end)
                count = repository.upsert_many("market_intraday_prices", [dict(row, instrument_id=instrument_id) for row in rows])
                connection.commit()
                result["symbols"][product] = {"missing_candles": sum(int((right-left).total_seconds()//FIVE_MINUTES) for left,right in windows), "upserted": count}
            except Exception as exc:
                connection.rollback()
                result["status"] = "partial"
                result["failures"].append({"symbol": product, "error": str(exc)})
        return result
    finally:
        if own_connection:
            connection.close()


def main() -> int:
    result = update(arguments())
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if result["status"] == "partial" else 0


if __name__ == "__main__":
    raise SystemExit(main())
