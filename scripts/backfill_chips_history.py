"""Backfill and incrementally maintain market-wide chips history.

Only complete rows are committed. Existing rows survive source failures, and
atomic checkpoints make an interrupted run safe to resume.
"""

from __future__ import annotations

import argparse
import csv
import logging
import os
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Mapping

from backfill_history import (
    RateLimitedSession,
    SourceError,
    TAIFEX_POSITION_URL,
    TAIPEI_TZ,
    load_taiwan_trading_days,
)
from config import PROJECT_ROOT
from providers.taifex_provider import TaifexProvider, _TableParser
from providers.twse_margin_provider import TwseMarginProvider
from providers.twse_provider import TwseProvider


LOGGER = logging.getLogger("chips_history")
OUTPUT_PATH = PROJECT_ROOT / "data" / "history" / "chips_daily.csv"
MARGIN_URL = TwseMarginProvider.source_url
# TWSE's official historical-compatible route; the /rwd route can redirect to
# an HTML rate-limit page during long sequential backfills.
INSTITUTIONAL_HISTORY_URL = "https://www.twse.com.tw/fund/BFI82U"
FIELDS = (
    "trade_date",
    "foreign_net",
    "investment_trust_net",
    "dealer_net",
    "foreign_futures_long",
    "foreign_futures_short",
    "foreign_futures_net",
    "margin_balance",
    "margin_change",
    "short_balance",
    "short_change",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Backfill market chips history")
    parser.add_argument("--days", type=int, default=250, help="most recent trading days")
    parser.add_argument("--end", default="today", help="YYYY-MM-DD or today")
    parser.add_argument("--delay", type=float, default=0.2)
    parser.add_argument("--retries", type=int, default=2)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--checkpoint", type=int, default=10)
    parser.add_argument("--refresh", action="store_true")
    return parser.parse_args()


def parse_end(value: str) -> date:
    if value.lower() == "today":
        return datetime.now(TAIPEI_TZ).date()
    return date.fromisoformat(value)


def load_existing() -> dict[str, dict[str, str]]:
    if not OUTPUT_PATH.exists():
        return {}
    with OUTPUT_PATH.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames != list(FIELDS):
            raise ValueError(f"Unexpected columns in {OUTPUT_PATH}")
        rows: dict[str, dict[str, str]] = {}
        for row in reader:
            key = row.get("trade_date", "")
            if not key or key in rows or any(row.get(field, "") == "" for field in FIELDS):
                raise ValueError("chips_daily.csv contains a missing or duplicate row")
            rows[key] = row
        return rows


def atomic_write(rows: Mapping[str, Mapping[str, Any]]) -> None:
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = OUTPUT_PATH.with_name(f".{OUTPUT_PATH.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=FIELDS, extrasaction="raise")
            writer.writeheader()
            writer.writerows(rows[key] for key in sorted(rows))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, OUTPUT_PATH)
    finally:
        if temporary.exists():
            temporary.unlink()


def fetch_institutional(http: RateLimitedSession, trade_date: date) -> Mapping[str, Any]:
    day = trade_date.strftime("%Y%m%d")
    payload = http.get_json(
        INSTITUTIONAL_HISTORY_URL,
        params={"dayDate": day, "response": "json"},
    )
    if not isinstance(payload, dict) or payload.get("date") != day or payload.get("stat") != "OK":
        raise SourceError("TWSE institutional data unavailable or date mismatch")
    provider = TwseProvider()
    records = provider.normalize(payload)
    if not provider.validate(records):
        raise SourceError("TWSE institutional validation failed")
    return records[0]


def fetch_futures(http: RateLimitedSession, trade_date: date) -> Mapping[str, Any]:
    response = http.request(
        "POST",
        TAIFEX_POSITION_URL,
        data={
            "queryType": "1",
            "goDay": "",
            "doQuery": "1",
            "dateaddcnt": "",
            "queryDate": trade_date.strftime("%Y/%m/%d"),
            "commodityId": "TXF",
        },
    )
    response.encoding = "utf-8"
    parser = _TableParser()
    parser.feed(response.text)
    provider = TaifexProvider()
    parsed_date = provider._extract_trade_date(parser.visible_text)
    if parsed_date != trade_date.isoformat():
        raise SourceError("TAIFEX foreign futures data unavailable or date mismatch")
    records = provider.normalize({"trade_date": parsed_date, "rows": parser.rows})
    if not provider.validate(records):
        raise SourceError("TAIFEX foreign futures validation failed")
    return records[0]


def fetch_margin(http: RateLimitedSession, trade_date: date) -> Mapping[str, Any]:
    day = trade_date.strftime("%Y%m%d")
    response = http.request(
        "GET",
        MARGIN_URL,
        params={"response": "json", "selectType": "MS", "date": day},
    )
    response.encoding = "utf-8"
    payload = response.json()
    if not isinstance(payload, dict) or payload.get("date") != day or payload.get("stat") != "OK":
        raise SourceError("TWSE margin data unavailable or date mismatch")
    provider = TwseMarginProvider()
    records = provider.normalize(payload)
    if not provider.validate(records):
        raise SourceError("TWSE margin validation failed")
    return records[0]


def build_row(http: RateLimitedSession, trade_date: date) -> dict[str, Any]:
    try:
        institutional = fetch_institutional(http, trade_date)
    except Exception as exc:
        raise SourceError(f"TWSE institutional: {exc}") from exc
    try:
        futures = fetch_futures(http, trade_date)
    except Exception as exc:
        raise SourceError(f"TAIFEX foreign futures: {exc}") from exc
    try:
        margin = fetch_margin(http, trade_date)
    except Exception as exc:
        raise SourceError(f"TWSE margin: {exc}") from exc
    amount = margin["margin_financing"]["amount"]
    short = margin["short_selling"]["trading_units"]
    row = {
        "trade_date": trade_date.isoformat(),
        "foreign_net": institutional["foreign_and_mainland_investors"]["net"],
        "investment_trust_net": institutional["investment_trust"]["net"],
        "dealer_net": institutional["dealers"]["net"],
        "foreign_futures_long": futures["long_position"]["open_interest"],
        "foreign_futures_short": futures["short_position"]["open_interest"],
        "foreign_futures_net": futures["net_position"]["open_interest"],
        "margin_balance": amount["balance"],
        "margin_change": amount["change"],
        "short_balance": short["balance"],
        "short_change": short["change"],
    }
    if any(value is None or value == "" for value in row.values()):
        raise SourceError("chips history row contains missing values")
    return row


def run(args: argparse.Namespace) -> int:
    if args.days < 20:
        raise ValueError("--days must be at least 20")
    end = parse_end(args.end)
    # 500 calendar days safely covers 250 Taiwan trading sessions.
    start = end - timedelta(days=max(args.days * 2, 500))
    http = RateLimitedSession(args.delay, args.retries, args.timeout)
    trading_days, _ = load_taiwan_trading_days(http, start, end)
    selected = trading_days[-args.days:]
    if len(selected) < args.days:
        raise SourceError(f"only {len(selected)} official trading dates are available")

    rows = load_existing()
    failures = 0
    processed = 0
    updated = 0
    for position, trade_date in enumerate(selected, 1):
        key = trade_date.isoformat()
        if key in rows and not args.refresh:
            continue
        LOGGER.info("[%d/%d] Updating %s", position, len(selected), key)
        try:
            rows[key] = build_row(http, trade_date)
            updated += 1
        except Exception as exc:
            failures += 1
            LOGGER.warning("%s preserved/skipped: %s", key, exc)
        processed += 1
        if processed % max(args.checkpoint, 1) == 0:
            atomic_write(rows)
    atomic_write(rows)
    LOGGER.info(
        "Chips history complete | requested=%d total=%d updated=%d failures=%d",
        len(selected), len(rows), updated, failures,
    )
    return 0 if rows else 1


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(message)s")
    try:
        return run(parse_args())
    except KeyboardInterrupt:
        LOGGER.warning("Interrupted; last atomic checkpoint remains valid")
        return 130
    except Exception as exc:
        LOGGER.error("Chips history failed: %s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
