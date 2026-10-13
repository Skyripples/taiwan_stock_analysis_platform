"""Stream Coinbase 5-minute Parquet staging into market_intraday_prices."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from config import PROJECT_ROOT
from crypto_market import FIVE_MINUTES, PRODUCTS, utc_datetime, validate_candle
from database.connection import connect
from database.global_market_repository import GlobalMarketRepository

UTC = timezone.utc
ROOT = PROJECT_ROOT / "data_lake" / "staging" / "crypto"


def boundary(value: str, *, inclusive_end: bool = False) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    date_only = "T" not in value and " " not in value
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    parsed = parsed.astimezone(UTC)
    return parsed + timedelta(days=1) if inclusive_end and date_only else parsed


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Import staged Coinbase 5-minute history into PostgreSQL")
    parser.add_argument("--symbols", nargs="+", choices=tuple(PRODUCTS), default=list(PRODUCTS))
    parser.add_argument("--start")
    parser.add_argument("--end")
    parser.add_argument("--batch-size", type=int, default=5000)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--verify", action="store_true")
    options = parser.parse_args()
    if options.batch_size < 1 or options.batch_size > 50000:
        parser.error("--batch-size must be between 1 and 50000")
    return options


def paths(symbols: list[str]) -> list[Path]:
    return [path for symbol in symbols for path in sorted((ROOT / f"symbol={symbol}").glob("year=*/month=*/candles.parquet"))]


def staged_rows(files: Iterable[Path], symbols: set[str], start: datetime | None, end: datetime | None, batch_size: int):
    import pyarrow.parquet as pq
    for path in files:
        parquet = pq.ParquetFile(path)
        for batch in parquet.iter_batches(batch_size=batch_size):
            output = []
            for raw in batch.to_pylist():
                symbol = str(raw.get("symbol") or "").upper()
                timestamp = utc_datetime(raw["timestamp_utc"])
                if symbol not in symbols or (start and timestamp < start) or (end and timestamp >= end):
                    continue
                quality = json.loads(raw.get("quality_flags") or "{}") if isinstance(raw.get("quality_flags"), str) else raw.get("quality_flags") or {}
                source_metadata = json.loads(raw.get("metadata") or "{}") if isinstance(raw.get("metadata"), str) else raw.get("metadata") or {}
                row = validate_candle(raw)
                output.append({"symbol": symbol, "timestamp_utc": timestamp, "interval_seconds": FIVE_MINUTES,
                    "open": row.get("open"), "high": row.get("high"), "low": row.get("low"), "close": row.get("close"), "volume": row.get("volume"),
                    "timezone": "UTC", "available_at": utc_datetime(raw["available_at"]), "quality_flags": quality,
                    "metadata": {**source_metadata, "source_key": "coinbase", "source_name": raw.get("source") or "Coinbase Exchange",
                                 "source_fetched_at": raw.get("fetched_at"), "source_symbol": symbol}})
            if output:
                yield output


def verify(connection, symbols: list[str]) -> dict[str, Any]:
    with connection.cursor() as cursor:
        cursor.execute("""SELECT mi.source_symbol AS symbol,count(p.timestamp_utc) AS rows,min(p.timestamp_utc) AS first_timestamp,
                                  max(p.timestamp_utc) AS last_timestamp
                           FROM market_instruments mi LEFT JOIN market_intraday_prices p
                             ON p.instrument_id=mi.instrument_id AND p.interval_seconds=%s
                           JOIN data_sources ds USING(source_id)
                           WHERE mi.source_symbol=ANY(%s) AND ds.source_key=%s
                           GROUP BY mi.source_symbol ORDER BY mi.source_symbol""",
                       (FIVE_MINUTES, symbols, "coinbase"))
        return {"source": "coinbase", "interval_seconds": FIVE_MINUTES,
                "symbols": [{key: value.isoformat() if isinstance(value, datetime) else value for key, value in row.items()} for row in cursor.fetchall()]}


def run(options: argparse.Namespace) -> dict[str, Any]:
    start = boundary(options.start) if options.start else None
    end = boundary(options.end, inclusive_end=True) if options.end else None
    if start and end and start >= end:
        raise ValueError("start must be earlier than end")
    files = paths(options.symbols)
    if options.verify:
        connection = connect()
        try: return {"mode": "verify", **verify(connection, options.symbols)}
        finally: connection.close()
    selected = duplicates = 0
    connection = None if options.dry_run else connect()
    repository = GlobalMarketRepository(connection) if connection else None
    instrument_ids: dict[str, int] = {}
    try:
        if connection:
            with connection.cursor() as cursor:
                cursor.execute("""SELECT mi.source_symbol,mi.instrument_id FROM market_instruments mi
                                  JOIN data_sources ds USING(source_id)
                                  WHERE mi.source_symbol=ANY(%s) AND ds.source_key=%s""", (options.symbols, "coinbase"))
                instrument_ids = {row["source_symbol"]: row["instrument_id"] for row in cursor.fetchall()}
            missing = set(options.symbols) - set(instrument_ids)
            if missing: raise RuntimeError("Missing Coinbase market instruments: " + ", ".join(sorted(missing)))
            connection.commit()
        for batch in staged_rows(files, set(options.symbols), start, end, options.batch_size):
            unique = []
            batch_seen: set[tuple[str, datetime]] = set()
            for row in batch:
                key = (row["symbol"], row["timestamp_utc"])
                if key in batch_seen: duplicates += 1; continue
                batch_seen.add(key); unique.append(row)
            selected += len(unique)
            if repository and unique:
                db_rows = [{key: value for key, value in row.items() if key != "symbol"} | {"instrument_id": instrument_ids[row["symbol"]]} for row in unique]
                try:
                    repository.upsert_many("market_intraday_prices", db_rows)
                    connection.commit()
                except Exception:
                    connection.rollback()
                    raise
        return {"mode": "dry-run" if options.dry_run else "import", "source": "coinbase", "interval_seconds": FIVE_MINUTES,
                "files": len(files), "rows": selected, "duplicates": duplicates,
                "start": start.isoformat() if start else None, "end_exclusive": end.isoformat() if end else None,
                "symbols": options.symbols}
    finally:
        if connection: connection.close()


def main() -> int:
    result = run(arguments())
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
