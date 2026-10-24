"""Incrementally update completed TAIFEX daily futures sessions in PostgreSQL."""
from __future__ import annotations

import argparse
import json
import logging
import random
import time
from datetime import date, datetime, time as wall_time, timedelta
from zoneinfo import ZoneInfo

import requests

from backfill_futures_history import (
    OfficialEmpty,
    PERSISTENT_OFFICIAL_EMPTY,
    PRODUCT_LISTING_DATES,
    RequestController,
    STAGING as FUTURES_DAILY_STAGING,
    fetch_day,
    history_client,
    read_rows as read_staged_rows,
    write_rows as write_staged_rows,
)
from database.connection import connect
from import_futures_history import FIELDS, normalized
from trading_calendar import get_trading_days_in_range

TAIPEI = ZoneInfo("Asia/Taipei")
PRODUCTS = ("TX", "MTX", "TMF")
PUBLISH_CUTOFF = wall_time(15, 30)
LOGGER = logging.getLogger("futures-daily-update")


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--products", nargs="+", choices=PRODUCTS, default=list(PRODUCTS))
    parser.add_argument("--lookback-days", type=int, default=14)
    parser.add_argument("--delay", type=float, default=2.5)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--staging-only", action="store_true",
                        help="Refresh local TX PIT staging without connecting to PostgreSQL")
    return parser.parse_args()


def latest_completed_trading_day(now: datetime | None = None) -> date:
    current = (now or datetime.now(TAIPEI)).astimezone(TAIPEI)
    candidate = current.date() if current.time() >= PUBLISH_CUTOFF else current.date() - timedelta(days=1)
    sessions, _ = get_trading_days_in_range(candidate - timedelta(days=14), candidate)
    if not sessions:
        raise ValueError("No completed Taiwan trading session in covered calendar range")
    return sessions[-1]


def read_db_state(connection, products: list[str], start: date, end: date) -> tuple[dict[str, date | None], set[tuple[str, date]]]:
    with connection.cursor() as cursor:
        cursor.execute(
            """SELECT product,max(trade_date) AS latest_date
                 FROM futures_daily_prices WHERE product=ANY(%s) AND session='regular'
                GROUP BY product""",
            (products,),
        )
        latest = {product: None for product in products}
        latest.update({row["product"]: row["latest_date"] for row in cursor.fetchall()})
        cursor.execute(
            """SELECT DISTINCT product,trade_date FROM futures_daily_prices
                WHERE product=ANY(%s) AND session='regular' AND contract_rank='front'
                  AND trade_date BETWEEN %s AND %s""",
            (products, start, end),
        )
        existing = {(row["product"], row["trade_date"]) for row in cursor.fetchall()}
    return latest, existing


def plan_jobs(
    products: list[str],
    sessions: list[date],
    existing: set[tuple[str, date]],
    *,
    refresh_products: set[str] | None = None,
) -> tuple[list[tuple[str, date]], int, int]:
    """Plan missing rows plus a small, explicit refresh window.

    TX is refreshed in the caller's normal lookback window so the local
    Data Lake remains an authoritative PIT input even when PostgreSQL already
    contains the same date.  Other products retain the previous missing-only
    behaviour.
    """
    refresh_products = refresh_products or set()
    jobs: list[tuple[str, date]] = []
    skipped_existing = 0
    skipped_persistent = 0
    for day in sessions:
        for product in products:
            if day < PRODUCT_LISTING_DATES.get(product, date.min):
                continue
            if (product, day) in PERSISTENT_OFFICIAL_EMPTY:
                skipped_persistent += 1
            elif (product, day) in existing and product not in refresh_products:
                skipped_existing += 1
            else:
                jobs.append((product, day))
    return jobs, skipped_existing, skipped_persistent


def upsert_rows(connection, rows: list[dict]) -> int:
    placeholders = ",".join(["%s"] * len(FIELDS))
    updates = ",".join(
        f"{name}=EXCLUDED.{name}" for name in FIELDS
        if name not in {"trade_date", "product", "contract", "session"}
    ) + ",updated_at=now()"
    statement = (
        f"INSERT INTO futures_daily_prices ({','.join(FIELDS)}) VALUES ({placeholders}) "
        f"ON CONFLICT (trade_date,product,contract,session) DO UPDATE SET {updates}"
    )
    with connection.transaction(), connection.cursor() as cursor:
        cursor.executemany(statement, [normalized(row) for row in rows])
    return len(rows)


def fetch_with_retry(controller: RequestController, day: date, product: str, *, sleep=time.sleep) -> list[dict]:
    for attempt, delay in enumerate((0, 5, 10, 20)):
        if delay:
            sleep(delay + random.uniform(0, 0.75))
        try:
            return fetch_day(controller, day, product)
        except (requests.Timeout, requests.ConnectionError, requests.HTTPError):
            if attempt == 3:
                raise
    raise RuntimeError("unreachable")


def sync_tx_staging(rows: list[dict], *, staging_root=FUTURES_DAILY_STAGING) -> dict:
    """Atomically merge freshly confirmed TX rows into PIT daily staging.

    ``backfill_futures_history.write_rows`` owns the canonical key and atomic
    gzip replacement behaviour.  Keeping this writer shared guarantees the
    incremental updater emits exactly the same JSONL schema as the historical
    backfill used by the PIT reader.
    """
    tx_rows = [row for row in rows if row.get("product") == "TX"]
    by_year: dict[int, list[dict]] = {}
    for row in tx_rows:
        year = date.fromisoformat(str(row["trade_date"])).year
        by_year.setdefault(year, []).append(row)

    updated_dates: set[str] = set()
    for year, year_rows in by_year.items():
        path = staging_root / "product=TX" / f"year={year}" / "daily.jsonl.gz"
        write_staged_rows(path, read_staged_rows(path) + year_rows)
        updated_dates.update(str(row["trade_date"]) for row in year_rows)

    return {
        "rows": len(tx_rows),
        "updated_dates": sorted(updated_dates),
        "partitions": len(by_year),
    }


def tx_staging_dates(*, staging_root=FUTURES_DAILY_STAGING) -> set[date]:
    """Return completed TX session dates already present in local staging."""
    dates: set[date] = set()
    for path in staging_root.glob("product=TX/year=*/daily.jsonl.gz"):
        dates.update(date.fromisoformat(str(row["trade_date"])) for row in read_staged_rows(path))
    return dates


def is_trailing_not_yet_published(
    product: str, day: date, jobs: list[tuple[str, date]], staged_tx_dates: set[date],
) -> bool:
    """Only a missing final TX session may be a normal publication delay."""
    if product != "TX" or day in staged_tx_dates:
        return False
    tx_jobs = [job_day for job_product, job_day in jobs if job_product == "TX"]
    return bool(tx_jobs) and day == max(tx_jobs)


def run_staging_only(options: argparse.Namespace, *, now: datetime | None = None, fetcher=fetch_with_retry) -> dict:
    """Refresh only the bounded TX Data Lake mirror used by PIT research.

    This is intentionally a convenience mode for local research.  The default
    updater continues to UPSERT PostgreSQL first and then mirror its confirmed
    TX rows into staging.
    """
    completed = latest_completed_trading_day(now)
    start = completed - timedelta(days=max(1, options.lookback_days))
    sessions, _ = get_trading_days_in_range(start, completed)
    jobs, _, skipped_persistent = plan_jobs(["TX"], sessions, set(), refresh_products={"TX"})
    staged_tx_dates = tx_staging_dates()
    summary = {
        "mode": "staging_only", "latest_completed_trading_day": completed.isoformat(),
        "planned_dates": [day.isoformat() for _, day in jobs], "staging_rows": 0,
        "staging_updated_dates": [], "skipped_persistent_official_empty": skipped_persistent,
        "skipped_not_yet_published": [],
        "failures": [],
    }
    if options.dry_run:
        summary.update({"dry_run": True, "network_requests": 0, "planned_jobs": len(jobs)})
        return summary
    controller = RequestController(history_client(), options.delay)
    for _, day in jobs:
        try:
            staging = sync_tx_staging(fetcher(controller, day, "TX"))
            summary["staging_rows"] += staging["rows"]
            summary["staging_updated_dates"].extend(staging["updated_dates"])
        except OfficialEmpty as exc:
            if is_trailing_not_yet_published("TX", day, jobs, staged_tx_dates):
                summary["skipped_not_yet_published"].append({
                    "product": "TX", "date": day.isoformat(), "reason": "not_yet_published",
                })
            else:
                summary["failures"].append({"product": "TX", "date": day.isoformat(), "error": str(exc), "retryable": True})
        except Exception as exc:
            summary["failures"].append({"product": "TX", "date": day.isoformat(), "error": str(exc), "retryable": True})
    summary["staging_updated_dates"] = sorted(set(summary["staging_updated_dates"]))
    summary["retry_count"] = controller.retry_count
    summary["rate_limit_count"] = controller.rate_limit_count
    return summary


def run(options: argparse.Namespace, *, now: datetime | None = None, fetcher=fetch_with_retry) -> dict:
    completed = latest_completed_trading_day(now)
    connection = connect()
    try:
        initial_start = completed - timedelta(days=max(1, options.lookback_days))
        latest, _ = read_db_state(connection, options.products, initial_start, completed)
        dated = [value for value in latest.values() if value is not None]
        start = min(initial_start, min(dated) + timedelta(days=1)) if dated else completed
        sessions, _ = get_trading_days_in_range(start, completed)
        latest, existing = read_db_state(connection, options.products, start, completed)
        # psycopg opens an implicit transaction for the planning SELECTs. End it
        # before the per-product transactions so each successful product/date is
        # committed independently instead of being rolled back on close.
        commit = getattr(connection, "commit", None)
        if commit is not None:
            commit()
        # Refresh TX over this deliberately small window even when PostgreSQL
        # already has it.  The same official response then keeps the Data Lake
        # staging input used by the PIT builder current and repairs a missed
        # local staging write without a historical backfill.
        refreshed_products = {"TX"} if "TX" in options.products else set()
        jobs, skipped_existing, skipped_persistent = plan_jobs(
            options.products, sessions, existing, refresh_products=refreshed_products,
        )
        staged_tx_dates = tx_staging_dates() if "TX" in options.products else set()
        summary = {
            "run_time": datetime.now(TAIPEI).isoformat(),
            "latest_completed_trading_day": completed.isoformat(),
            "planned_dates": sorted({day.isoformat() for _, day in jobs}),
            "products": options.products,
            "downloaded_rows": 0,
            "downloaded_by_product": {product: 0 for product in options.products},
            "staging_rows": 0,
            "staging_updated_dates": [],
            "skipped_not_yet_published": [],
            "skipped_existing": skipped_existing,
            "skipped_persistent_official_empty": skipped_persistent,
            "failures": [],
            "db_latest_before": {key: value.isoformat() if value else None for key, value in latest.items()},
        }
        if options.dry_run:
            summary.update({"dry_run": True, "network_requests": 0, "planned_jobs": len(jobs)})
            return summary
        controller = RequestController(history_client(), options.delay)
        for product, day in jobs:
            try:
                rows = fetcher(controller, day, product)
                count = upsert_rows(connection, rows)
                summary["downloaded_rows"] += count
                summary["downloaded_by_product"][product] += count
                if product == "TX":
                    staging = sync_tx_staging(rows)
                    summary["staging_rows"] += staging["rows"]
                    summary["staging_updated_dates"].extend(staging["updated_dates"])
            except OfficialEmpty as exc:
                if is_trailing_not_yet_published(product, day, jobs, staged_tx_dates):
                    summary["skipped_not_yet_published"].append({
                        "product": product, "date": day.isoformat(), "reason": "not_yet_published",
                    })
                else:
                    summary["failures"].append({
                        "product": product, "date": day.isoformat(), "error": str(exc), "retryable": True,
                    })
            except Exception as exc:
                summary["failures"].append({
                    "product": product, "date": day.isoformat(),
                    "error": str(exc), "retryable": True,
                })
        latest_after, _ = read_db_state(connection, options.products, start, completed)
        summary["db_latest_after"] = {key: value.isoformat() if value else None for key, value in latest_after.items()}
        summary["retry_count"] = controller.retry_count
        summary["rate_limit_count"] = controller.rate_limit_count
        summary["staging_updated_dates"] = sorted(set(summary["staging_updated_dates"]))
        return summary
    finally:
        connection.close()


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(message)s")
    try:
        options = arguments()
        summary = run_staging_only(options) if options.staging_only else run(options)
        print(json.dumps(summary, ensure_ascii=False))
        return 2 if summary.get("failures") else 0
    except Exception as exc:
        LOGGER.exception("Futures daily update failed: %s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
