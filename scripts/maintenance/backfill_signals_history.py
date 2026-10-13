"""Add missing legacy signal-history rows from canonical historical market rows."""
from __future__ import annotations

import csv
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MARKET = ROOT / "data/history/backfill_market_daily.csv"
SIGNALS = ROOT / "data/history/signals_daily.csv"
NAMES = (
    ("foreign_cash_flow", "foreign_cash_flow"),
    ("foreign_futures_position", "foreign_futures_position"),
    ("night_futures", "night_futures_change"),
    ("tsm_adr", "tsm_adr_change_percent"),
    ("sox_index", "sox_change_percent"),
)
DATE_FIELDS = (
    "taiwan_market_trade_date", "institutional_trade_date", "foreign_futures_trade_date",
    "night_futures_trade_date", "tsm_adr_trade_date", "sox_trade_date",
    "sp500_trade_date", "nasdaq_trade_date",
)


def classify(value: str) -> tuple[str, int]:
    number = float(value)
    return ("bullish", 1) if number > 0 else (("bearish", -1) if number < 0 else ("neutral", 0))


def run() -> int:
    with SIGNALS.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle); fields = list(reader.fieldnames or ()); existing = list(reader)
    by_date = {row["trade_date"]: row for row in existing}
    with MARKET.open("r", encoding="utf-8-sig", newline="") as handle:
        historical = list(csv.DictReader(handle))
    added = 0
    for source in historical:
        key = source["trade_date"]
        if key in by_date:
            continue
        row = {"trade_date": key, **{field: source[field] for field in DATE_FIELDS}}
        total = 0
        for name, value_field in NAMES:
            status, score = classify(source[value_field]); total += score
            row[f"{name}_status"] = status; row[f"{name}_score"] = score
        row.update({"market_score": total, "max_score": 5, "percentage": (total + 5) * 10,
                    "market_status": "Bullish" if total > 0 else ("Bearish" if total < 0 else "Neutral")})
        by_date[key] = row; added += 1
    if not added:
        return 0
    backup = SIGNALS.with_name(f"{SIGNALS.name}.{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.bak")
    shutil.copy2(SIGNALS, backup)
    temporary = SIGNALS.with_name(f".{SIGNALS.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="raise", lineterminator="\n")
            writer.writeheader(); writer.writerows(by_date[key] for key in sorted(by_date)); handle.flush(); os.fsync(handle.fileno())
        os.replace(temporary, SIGNALS)
    finally:
        if temporary.exists(): temporary.unlink()
    return added


if __name__ == "__main__":
    print(f"Historical signal rows added: {run()}")
