"""PC-side, resumable Coinbase 5-minute history backfill to Parquet staging."""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from config import PROJECT_ROOT
from crypto_market import (FIVE_MINUTES, PRODUCTS, SOURCE, completed_candle_end,
                           missing_candle_windows, read_parquet, utc_datetime,
                           write_json_atomic, write_parquet_atomic)
from research.backfill_global_market import Client, fetch_crypto_chunk

UTC = timezone.utc
ROOT = PROJECT_ROOT / "data_lake" / "staging" / "crypto"
CHECKPOINT = ROOT / "checkpoint.json"
SUMMARY = ROOT / "summary.json"


def arguments() -> argparse.Namespace:
    def boundary(value: str) -> datetime:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)

    parser = argparse.ArgumentParser(description="Backfill Coinbase BTC/ETH 5-minute OHLCV to local staging")
    parser.add_argument("--symbols", nargs="+", choices=tuple(PRODUCTS), default=list(PRODUCTS))
    parser.add_argument("--years", type=int, default=5)
    parser.add_argument("--start", type=boundary)
    parser.add_argument("--end", type=boundary)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def month_ranges(start: datetime, end: datetime):
    cursor = datetime(start.year, start.month, 1, tzinfo=UTC)
    while cursor < end:
        following = datetime(cursor.year + (cursor.month == 12), 1 if cursor.month == 12 else cursor.month + 1, 1, tzinfo=UTC)
        yield max(start, cursor), min(end, following)
        cursor = following


def partition(symbol: str, moment: datetime) -> Path:
    return ROOT / f"symbol={symbol}" / f"year={moment.year}" / f"month={moment.month:02d}" / "candles.parquet"


def run(options: argparse.Namespace, client: Any | None = None) -> dict[str, Any]:
    end = min(completed_candle_end(options.end) if options.end else completed_candle_end(), completed_candle_end())
    start = completed_candle_end(options.start) if options.start else end - timedelta(days=365 * options.years)
    if start >= end:
        raise ValueError("start must be earlier than the last completed candle")
    plan: dict[str, Any] = {}
    total_downloaded = total_skipped = duplicates = 0
    failures: list[dict[str, Any]] = []
    for symbol in options.symbols:
        missing = requests = existing_count = 0
        for chunk_start, chunk_end in month_ranges(start, end):
            existing_rows = read_parquet(partition(symbol, chunk_start))
            existing = {utc_datetime(row["timestamp_utc"]) for row in existing_rows}
            windows = missing_candle_windows(chunk_start, chunk_end, existing)
            missing += sum(int((right - left).total_seconds() // FIVE_MINUTES) for left, right in windows)
            requests += len(windows)
            existing_count += len(existing)
        plan[symbol] = {"existing_candles": existing_count, "missing_candles": missing, "estimated_requests": requests}
    if options.dry_run:
        return {"status": "dry-run", "network_requests": 0, "start": start.isoformat(), "end": end.isoformat(), "plan": plan}
    client = client or Client()
    for symbol in options.symbols:
        for chunk_start, chunk_end in month_ranges(start, end):
            path = partition(symbol, chunk_start)
            existing_rows = read_parquet(path)
            existing = {utc_datetime(row["timestamp_utc"]) for row in existing_rows}
            windows = missing_candle_windows(chunk_start, chunk_end, existing)
            total_skipped += len(existing)
            for left, right in windows:
                try:
                    fetched = fetch_crypto_chunk(client, symbol, left, right, FIVE_MINUTES)
                    now = datetime.now(UTC).isoformat()
                    rows = [{**row, "symbol": symbol, "source": SOURCE, "fetched_at": now} for row in fetched if utc_datetime(row["timestamp_utc"]) < end]
                    before = len(existing_rows)
                    if rows:
                        write_parquet_atomic(path, [*existing_rows, *rows])
                        existing_rows = read_parquet(path)
                        total_downloaded += len(rows)
                        duplicates += max(0, before + len(rows) - len(existing_rows))
                        existing = {utc_datetime(row["timestamp_utc"]) for row in existing_rows}
                    else:
                        failures.append({"symbol": symbol, "start": left.isoformat(), "end": right.isoformat(), "error": "official-empty"})
                    write_json_atomic(CHECKPOINT, {"status": "running", "symbol": symbol, "cursor": right.isoformat(), "downloaded_rows": total_downloaded, "failures": failures})
                except Exception as exc:
                    failures.append({"symbol": symbol, "start": left.isoformat(), "end": right.isoformat(), "error": str(exc)})
                    write_json_atomic(CHECKPOINT, {"status": "partial", "symbol": symbol, "cursor": left.isoformat(), "downloaded_rows": total_downloaded, "failures": failures})
    result = {"status": "partial" if failures else "complete", "start": start.isoformat(), "end": end.isoformat(), "symbols": options.symbols,
              "downloaded_rows": total_downloaded, "skipped_existing": total_skipped, "duplicates": duplicates,
              "missing_candles": sum(item["missing_candles"] for item in plan.values()), "failures": failures,
              "completed_at": datetime.now(UTC).isoformat()}
    write_json_atomic(SUMMARY, result)
    write_json_atomic(CHECKPOINT, result)
    return result


def main() -> int:
    import json
    result = run(arguments())
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if result.get("status") == "partial" else 0


if __name__ == "__main__":
    raise SystemExit(main())
