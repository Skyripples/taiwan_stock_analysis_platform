"""Daily idempotent TAIFEX official tick reconstruction updater."""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta

from backfill_futures_intraday_5m import run
from database.connection import connect
from trading_calendar import get_trading_days_in_range
from futures_intraday import TAIPEI


def arguments():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lookback-days", type=int, default=45); parser.add_argument("--delay", type=float, default=2.0)
    parser.add_argument("--dry-run", action="store_true"); return parser.parse_args()


def completed_day(now: datetime | None = None):
    current = (now or datetime.now(TAIPEI)).astimezone(TAIPEI)
    candidate = current.date() if current.hour >= 16 else current.date() - timedelta(days=1)
    sessions, _ = get_trading_days_in_range(candidate - timedelta(days=14), candidate)
    if not sessions: raise ValueError("No covered completed Taiwan trading day")
    return sessions[-1]


def main() -> int:
    options = arguments(); end = completed_day(); start = end - timedelta(days=max(1, options.lookback_days))
    options.start, options.end = start.isoformat(), end.isoformat(); options.products = ["TX", "MTX", "TMF"]
    options.resume = True; options.apply = not options.dry_run
    existing = set()
    if not options.dry_run:
        connection = connect()
        try:
            with connection.cursor() as cursor:
                cursor.execute("""SELECT DISTINCT product,trading_date FROM futures_intraday_prices
                                   WHERE interval_seconds=300 AND trading_date BETWEEN %s AND %s
                                     AND product=ANY(%s)""", (start, end, options.products))
                existing = {(row["product"], row["trading_date"]) for row in cursor.fetchall()}
        finally:
            connection.close()
    summary = run(options, existing_partitions=existing); print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 2 if any(item["status"] == "failed" for item in summary.get("failures", [])) else 0


if __name__ == "__main__": raise SystemExit(main())
