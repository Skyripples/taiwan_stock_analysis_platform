"""Pure futures calculations shared by collectors, API, and tests."""
from __future__ import annotations

import math
from datetime import date
from typing import Any

PRODUCTS = ("TX", "MTX", "TMF")
SESSIONS = ("regular", "after_hours")


def basis(futures_price: float | None, spot_index: float | None) -> tuple[float | None, float | None]:
    if futures_price is None or spot_index in (None, 0):
        return None, None
    value = float(futures_price) - float(spot_index)
    return value, value / float(spot_index) * 100


def historical_percentile(values: list[float | None], current: float | None) -> float | None:
    clean = [float(value) for value in values if value is not None and math.isfinite(float(value))]
    if current is None or len(clean) < 20:
        return None
    below = sum(value < float(current) for value in clean)
    equal = sum(value == float(current) for value in clean)
    return (below + equal / 2) / len(clean) * 100


def position_metrics(rows: list[dict[str, Any]]) -> dict[str, float | int | None]:
    ordered = sorted(rows, key=lambda row: str(row["trade_date"]))
    if not ordered:
        return {"foreign_net_oi": None, "foreign_net_oi_change_1d": None,
                "foreign_net_oi_change_5d": None, "foreign_net_oi_zscore": None}
    values = [row.get("net_oi") for row in ordered]
    latest = values[-1]
    change_1d = None if latest is None or len(values) < 2 or values[-2] is None else latest - values[-2]
    change_5d = None if latest is None or len(values) < 6 or values[-6] is None else latest - values[-6]
    valid = [float(value) for value in values[-20:] if value is not None]
    zscore = None
    if latest is not None and len(valid) >= 5:
        mean = sum(valid) / len(valid)
        variance = sum((value - mean) ** 2 for value in valid) / len(valid)
        zscore = None if variance == 0 else (float(latest) - mean) / math.sqrt(variance)
    return {"foreign_net_oi": latest, "foreign_net_oi_change_1d": change_1d,
            "foreign_net_oi_change_5d": change_5d, "foreign_net_oi_zscore": zscore}


def contract_metadata(trade_date: date, expiry_date: date | None) -> dict[str, Any]:
    if expiry_date is None:
        return {"days_to_expiry": None, "is_expiry_week": None}
    days = (expiry_date - trade_date).days
    return {"days_to_expiry": days, "is_expiry_week": 0 <= days <= 7}


def price_oi_state(price_change: float | None, oi_change: float | None) -> str | None:
    if price_change is None or oi_change is None or price_change == 0 or oi_change == 0:
        return None
    return ("價格上漲 / OI 增加" if price_change > 0 and oi_change > 0 else
            "價格上漲 / OI 減少" if price_change > 0 else
            "價格下跌 / OI 增加" if oi_change > 0 else "價格下跌 / OI 減少")
