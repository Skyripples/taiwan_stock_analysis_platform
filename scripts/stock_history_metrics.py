"""Pure, NULL-safe derived metrics for stock history API responses."""

from __future__ import annotations

from math import sqrt
from statistics import fmean, pstdev
from typing import Any


def percent_change(current: Any, previous: Any) -> float | None:
    if current is None or previous in {None, 0}:
        return None
    return round((float(current) / float(previous) - 1) * 100, 6)


def percentile(values: list[Any], current: Any) -> float | None:
    clean = sorted(float(value) for value in values if value is not None)
    if current is None or not clean:
        return None
    below = sum(value < float(current) for value in clean)
    equal = sum(value == float(current) for value in clean)
    return round((below + equal * 0.5) / len(clean) * 100, 6)


def revenue_metrics(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ordered = sorted((dict(row) for row in rows), key=lambda row: str(row["revenue_month"]))
    by_month = {str(row["revenue_month"])[:7]: row for row in ordered}
    positive_streak = 0
    for index, row in enumerate(ordered):
        month = str(row["revenue_month"])[:7]
        year, number = map(int, month.split("-"))
        previous_month = f"{year - (number == 1):04d}-{12 if number == 1 else number - 1:02d}"
        previous_year = f"{year - 1:04d}-{number:02d}"
        row["mom"] = row.get("revenue_mom") if row.get("revenue_mom") is not None else percent_change(row.get("revenue"), (by_month.get(previous_month) or {}).get("revenue"))
        row["yoy"] = row.get("revenue_yoy") if row.get("revenue_yoy") is not None else percent_change(row.get("revenue"), (by_month.get(previous_year) or {}).get("revenue"))
        recent_yoy = [item.get("yoy") for item in ordered[max(0, index - 2):index + 1] if item.get("yoy") is not None]
        row["average_yoy_3m"] = round(fmean(recent_yoy), 6) if len(recent_yoy) == 3 else None
        current_ytd = [item.get("revenue") for item in ordered if str(item["revenue_month"]).startswith(f"{year}-") and str(item["revenue_month"])[:7] <= month]
        prior_cutoff = f"{year - 1:04d}-{number:02d}"
        prior_ytd = [item.get("revenue") for item in ordered if str(item["revenue_month"]).startswith(f"{year - 1}-") and str(item["revenue_month"])[:7] <= prior_cutoff]
        row["ytd_revenue"] = sum(current_ytd) if len(current_ytd) == number and all(value is not None for value in current_ytd) else None
        prior_total = sum(prior_ytd) if len(prior_ytd) == number and all(value is not None for value in prior_ytd) else None
        row["ytd_yoy"] = percent_change(row["ytd_revenue"], prior_total)
        positive_streak = positive_streak + 1 if row.get("yoy") is not None and row["yoy"] > 0 else 0
        row["consecutive_positive_yoy_months"] = positive_streak
    return ordered


def chips_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    ordered = sorted(rows, key=lambda row: str(row["trade_date"]))
    def window_sum(field: str, days: int) -> int | float | None:
        values = [row.get(field) for row in ordered[-days:]]
        return sum(values) if len(values) == days and all(value is not None for value in values) else None
    def balance_change(field: str, days: int) -> int | float | None:
        return ordered[-1].get(field) - ordered[-1-days].get(field) if len(ordered) > days and ordered[-1].get(field) is not None and ordered[-1-days].get(field) is not None else None
    def streak(field: str) -> int:
        direction = 0; count = 0
        for row in reversed(ordered):
            value = row.get(field)
            sign = 1 if value is not None and value > 0 else -1 if value is not None and value < 0 else 0
            if not sign or (direction and sign != direction): break
            direction = sign; count += sign
        return count
    def zscore(field: str, days: int = 60) -> float | None:
        values = [float(row[field]) for row in ordered[-days:] if row.get(field) is not None]
        if len(values) < 20 or not pstdev(values): return None
        return round((values[-1] - fmean(values)) / pstdev(values), 6)
    output: dict[str, Any] = {}
    for field, prefix, windows in (("foreign_net", "foreign", (1,5,20,60)), ("investment_trust_net", "investment_trust", (5,20)), ("dealer_net", "dealer", (5,20)), ("institutional_total", "institutional", (5,20))):
        for days in windows: output[f"{prefix}_{days}d"] = window_sum(field, days)
        output[f"{prefix}_consecutive_days"] = streak(field)
        output[f"{prefix}_consecutive_buy_days"] = max(output[f"{prefix}_consecutive_days"],0)
        output[f"{prefix}_consecutive_sell_days"] = abs(min(output[f"{prefix}_consecutive_days"],0))
        output[f"{prefix}_zscore"] = zscore(field)
    for field, prefix in (("margin_balance", "margin"), ("short_balance", "short")):
        for days in (1,5,20): output[f"{prefix}_{days}d_change"] = balance_change(field, days)
        previous = ordered[-2].get(field) if len(ordered) > 1 else None
        output[f"{prefix}_balance_change_percent"] = percent_change(ordered[-1].get(field), previous) if ordered else None
    latest = ordered[-1] if ordered else {}
    margin, short = latest.get("margin_balance"), latest.get("short_balance")
    output["margin_short_ratio"] = round(float(margin) / float(short), 6) if margin is not None and short not in {None, 0} else None
    return output


def valuation_percentiles(rows: list[dict[str, Any]], minimum_observations: int = 20) -> dict[str, float | None]:
    if not rows: return {"pe": None, "pb": None, "dividend_yield": None}
    latest = max(rows, key=lambda row: str(row["valuation_date"]))
    output={}
    for field in ("pe", "pb", "dividend_yield"):
        values=[row.get(field) for row in rows if row.get(field) is not None]
        output[field]=percentile(values,latest.get(field)) if len(values)>=minimum_observations else None
    return output


def financial_metrics(row: dict[str, Any]) -> dict[str, Any]:
    """Recompute deterministic quarterly ratios without filling missing data."""
    result=dict(row)
    def ratio_value(numerator: Any,denominator: Any)->float|None:
        return round(float(numerator)/float(denominator)*100,6) if numerator is not None and denominator not in {None,0} else None
    result["gross_margin"]=ratio_value(result.get("gross_profit"),result.get("revenue"))
    result["operating_margin"]=ratio_value(result.get("operating_income"),result.get("revenue"))
    result["net_margin"]=ratio_value(result.get("net_income"),result.get("revenue"))
    result["debt_ratio"]=ratio_value(result.get("total_liabilities"),result.get("total_assets"))
    annual_net=float(result["net_income"])*4 if result.get("net_income") is not None else None
    result["roe"]=ratio_value(annual_net,result.get("equity",result.get("total_equity")))
    result["roa"]=ratio_value(annual_net,result.get("total_assets"))
    result["free_cash_flow"]=(result["operating_cash_flow"]-result["capital_expenditure"] if result.get("operating_cash_flow") is not None and result.get("capital_expenditure") is not None else None)
    return result
