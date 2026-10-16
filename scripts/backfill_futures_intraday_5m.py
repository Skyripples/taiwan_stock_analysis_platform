"""Reconstruct recent TX/MTX/TMF 5-minute candles from official TAIFEX daily tick ZIPs."""
from __future__ import annotations

import argparse
import json
import random
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests

from config import PROJECT_ROOT
from database.connection import connect, configured
from futures_intraday_reconstruction import aggregate_ticks, parquet_path, upsert_candles, write_parquet_atomic
from io_utils import atomic_write_json
from providers.taifex_tick_csv_provider import (OfficialTickFileUnavailable, PRODUCTS,
                                                 download_tick_zip, iter_product_ticks)
from trading_calendar import get_trading_days_in_range

STAGING = PROJECT_ROOT / "data_lake" / "staging" / "futures_intraday_5m"
CHECKPOINT = STAGING / "checkpoint.json"
SUMMARY = STAGING / "summary.json"


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", required=True); parser.add_argument("--end", required=True)
    parser.add_argument("--products", nargs="+", choices=PRODUCTS, default=list(PRODUCTS))
    parser.add_argument("--resume", action="store_true"); parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--apply", action="store_true", help="Also UPSERT completed candles into PostgreSQL")
    parser.add_argument("--delay", type=float, default=2.0)
    return parser.parse_args()


def load_checkpoint() -> dict:
    if not CHECKPOINT.exists(): return {"completed": {}}
    try:
        value = json.loads(CHECKPOINT.read_text(encoding="utf-8"))
        return value if isinstance(value.get("completed"), dict) else {"completed": {}}
    except (OSError, ValueError, TypeError): return {"completed": {}}


def completed(checkpoint: dict, day: date, product: str) -> bool:
    return product in checkpoint.get("completed", {}).get(day.isoformat(), []) and parquet_path(STAGING, day, product).exists()


def save_complete(checkpoint: dict, day: date, product: str) -> None:
    items = checkpoint.setdefault("completed", {}).setdefault(day.isoformat(), [])
    if product not in items: items.append(product); items.sort()
    checkpoint["updated_at"] = datetime.now(timezone.utc).isoformat(); atomic_write_json(CHECKPOINT, checkpoint)


def download_with_retry(day: date, session: requests.Session, *, sleep=time.sleep):
    for attempt, delay in enumerate((0, 5, 10, 20)):
        if delay: sleep(delay + random.uniform(0, .5))
        try: return download_tick_zip(day, session=session)
        except (requests.Timeout, requests.ConnectionError, requests.HTTPError):
            if attempt == 3: raise
    raise RuntimeError("unreachable")


def run(options: argparse.Namespace, *, downloader=download_with_retry,
        existing_partitions: set[tuple[str, date]] | None = None) -> dict:
    start, end = date.fromisoformat(options.start), date.fromisoformat(options.end)
    sessions, closed = get_trading_days_in_range(start, end); checkpoint = load_checkpoint() if options.resume else {"completed": {}}
    existing_partitions = existing_partitions or set()
    jobs = {day: [product for product in options.products if not (options.resume and (
        completed(checkpoint, day, product) or (product, day) in existing_partitions))] for day in sessions}
    jobs = {day: products for day, products in jobs.items() if products}
    summary = {"date_range": [options.start, options.end], "sessions": len(sessions),
               "planned_files": len(jobs), "planned_product_partitions": sum(map(len, jobs.values())),
               "downloaded_files": 0, "downloaded_rows": 0,
               "skipped_existing": len(sessions) * len(options.products) - sum(map(len, jobs.values())),
               "skipped_non_trading_days": len(closed), "failures": []}
    if options.dry_run:
        summary.update({"dry_run": True, "network_requests": 0}); return summary
    if options.apply and not configured(): raise RuntimeError("PostgreSQL environment is not configured")
    client = requests.Session(); connection = connect() if options.apply else None
    try:
        for index, (day, wanted) in enumerate(jobs.items()):
            if index and options.delay: time.sleep(options.delay)
            try:
                downloaded = downloader(day, client); summary["downloaded_files"] += 1
                all_candles = aggregate_ticks(iter_product_ticks(downloaded.content, wanted), day,
                                              available_at=downloaded.available_at)
                grouped = {product: [] for product in wanted}
                for candle in all_candles: grouped[candle.product].append(candle)
                for product in wanted:
                    try:
                        candles = grouped[product]
                        if not candles: raise ValueError(f"TAIFEX file contains no {product} ticks")
                        if not any(candle.session == "regular" for candle in candles):
                            raise OfficialTickFileUnavailable(
                                f"TAIFEX tick file for {day} is not final: {product} regular session is absent"
                            )
                        write_parquet_atomic(parquet_path(STAGING, day, product), candles)
                        if connection is not None: upsert_candles(connection, candles)
                        summary["downloaded_rows"] += len(candles); save_complete(checkpoint, day, product)
                    except OfficialTickFileUnavailable as exc:
                        summary["failures"].append({"date": day.isoformat(), "products": [product],
                                                    "status": "not-yet-published", "retryable": True, "error": str(exc)})
                    except Exception as exc:
                        summary["failures"].append({"date": day.isoformat(), "products": [product],
                                                    "status": "failed", "retryable": True, "error": str(exc)})
            except OfficialTickFileUnavailable as exc:
                summary["failures"].append({"date": day.isoformat(), "products": wanted, "status": "not-yet-published", "retryable": True, "error": str(exc)})
            except Exception as exc:
                summary["failures"].append({"date": day.isoformat(), "products": wanted, "status": "failed", "retryable": True, "error": str(exc)})
    finally:
        if connection is not None: connection.close()
    summary["completed_at"] = datetime.now(timezone.utc).isoformat(); atomic_write_json(SUMMARY, summary)
    return summary


def main() -> int:
    options = arguments(); summary = run(options); print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 2 if any(item["status"] == "failed" for item in summary.get("failures", [])) else 0


if __name__ == "__main__": raise SystemExit(main())
