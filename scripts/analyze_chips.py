"""Create deterministic market chips analysis from chips_daily.csv."""

from __future__ import annotations

import csv
import json
import os
from datetime import datetime, timezone
from statistics import fmean, pstdev
from typing import Any, Iterable

from config import PROJECT_ROOT


INPUT_PATH = PROJECT_ROOT / "data" / "history" / "chips_daily.csv"
OUTPUT_PATH = PROJECT_ROOT / "data" / "analysis" / "current" / "chips_summary.json"
NUMERIC_FIELDS = (
    "foreign_net", "investment_trust_net", "dealer_net",
    "foreign_futures_long", "foreign_futures_short", "foreign_futures_net",
    "margin_balance", "margin_change", "short_balance", "short_change",
)

# Centralized, deterministic first-version rules. No AI or narrative inference:
# institutional follows 5-day foreign+trust flow; futures follows 5-day net OI
# change; margin growth is contrarian because rapidly rising leverage is riskier.
STATUS_RULES = {
    "institutional": {"field": "five_day_combined_flow", "bullish_above": 0, "bearish_below": 0},
    "futures": {"field": "net_change_5d", "bullish_above": 0, "bearish_below": 0},
    "margin": {"field": "balance_change_5d", "bullish_below": 0, "bearish_above": 0},
}


def load_rows() -> list[dict[str, Any]]:
    with INPUT_PATH.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        rows: list[dict[str, Any]] = []
        previous = ""
        for raw in reader:
            trade_date = raw.get("trade_date", "")
            if not trade_date or trade_date <= previous:
                raise ValueError("chips history dates must be unique and strictly increasing")
            row: dict[str, Any] = {"trade_date": trade_date}
            for field in NUMERIC_FIELDS:
                value = raw.get(field, "")
                if value == "":
                    raise ValueError(f"missing {field} on {trade_date}")
                row[field] = int(value)
            rows.append(row)
            previous = trade_date
    if len(rows) < 21:
        raise ValueError("at least 21 complete chips history rows are required")
    return rows


def window_sum(rows: list[dict[str, Any]], field: str, size: int) -> int:
    return sum(row[field] for row in rows[-size:])


def change(rows: list[dict[str, Any]], field: str, periods: int) -> int:
    return rows[-1][field] - rows[-(periods + 1)][field]


def streak(values: Iterable[int]) -> dict[str, Any]:
    items = list(values)
    sign = 1 if items[-1] > 0 else -1 if items[-1] < 0 else 0
    days = 0
    for value in reversed(items):
        if (1 if value > 0 else -1 if value < 0 else 0) != sign:
            break
        days += 1
    return {"direction": "buy" if sign > 0 else "sell" if sign < 0 else "neutral", "days": days}


def zscore(rows: list[dict[str, Any]], field: str, size: int = 20) -> float:
    values = [row[field] for row in rows[-size:]]
    deviation = pstdev(values)
    return 0.0 if deviation == 0 else round((values[-1] - fmean(values)) / deviation, 6)


def classify(rule_name: str, value: int) -> str:
    rule = STATUS_RULES[rule_name]
    if "bullish_above" in rule and value > rule["bullish_above"]:
        return "bullish"
    if "bullish_below" in rule and value < rule["bullish_below"]:
        return "bullish"
    if "bearish_below" in rule and value < rule["bearish_below"]:
        return "bearish"
    if "bearish_above" in rule and value > rule["bearish_above"]:
        return "bearish"
    return "neutral"


def atomic_json(payload: dict[str, Any]) -> None:
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = OUTPUT_PATH.with_name(f".{OUTPUT_PATH.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, OUTPUT_PATH)
    finally:
        if temporary.exists():
            temporary.unlink()


def analyze(rows: list[dict[str, Any]]) -> dict[str, Any]:
    latest = rows[-1]
    institutional_5d = window_sum(rows, "foreign_net", 5) + window_sum(rows, "investment_trust_net", 5)
    futures_5d = change(rows, "foreign_futures_net", 5)
    margin_5d = change(rows, "margin_balance", 5)
    if futures_5d < 0:
        futures_action = "increasing_shorts"
    elif latest["foreign_futures_net"] < 0:
        futures_action = "covering_shorts"
    else:
        futures_action = "increasing_longs"
    return {
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "version": "1.0",
        "trade_date": latest["trade_date"],
        "sample_count": len(rows),
        "units": {"institutional_amount": "TWD", "futures_position": "contracts", "margin_balance": "thousand_TWD", "short_balance": "trading_units"},
        "institutional": {
            "foreign_streak": streak(row["foreign_net"] for row in rows),
            "investment_trust_streak": streak(row["investment_trust_net"] for row in rows),
            "foreign_5d_sum": window_sum(rows, "foreign_net", 5),
            "foreign_20d_sum": window_sum(rows, "foreign_net", 20),
            "investment_trust_5d_sum": window_sum(rows, "investment_trust_net", 5),
            "investment_trust_20d_sum": window_sum(rows, "investment_trust_net", 20),
            "foreign_20d_zscore": zscore(rows, "foreign_net"),
        },
        "futures": {
            "current_net_position": latest["foreign_futures_net"],
            "net_change_1d": change(rows, "foreign_futures_net", 1),
            "net_change_5d": futures_5d,
            "net_change_20d": change(rows, "foreign_futures_net", 20),
            "position_action": futures_action,
            "net_position_20d_zscore": zscore(rows, "foreign_futures_net"),
        },
        "margin": {
            "current_margin_balance": latest["margin_balance"],
            "current_short_balance": latest["short_balance"],
            "margin_change_1d": change(rows, "margin_balance", 1),
            "margin_change_5d": margin_5d,
            "margin_change_20d": change(rows, "margin_balance", 20),
            "short_change_1d": change(rows, "short_balance", 1),
            "short_change_5d": change(rows, "short_balance", 5),
            "short_change_20d": change(rows, "short_balance", 20),
            "margin_balance_20d_zscore": zscore(rows, "margin_balance"),
        },
        "statuses": {
            "institutional_status": classify("institutional", institutional_5d),
            "futures_status": classify("futures", futures_5d),
            "margin_status": classify("margin", margin_5d),
        },
        "status_rules": STATUS_RULES,
        "history": {"last_20": rows[-20:], "last_60": rows[-60:]},
    }


def main() -> int:
    try:
        payload = analyze(load_rows())
        atomic_json(payload)
        print(f"Chips summary updated: {OUTPUT_PATH} | rows={payload['sample_count']}")
        return 0
    except Exception as exc:
        print(f"Chips analysis failed; existing output preserved: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
