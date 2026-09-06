"""Validate common rule-based market hypotheses on existing PIT-aligned history."""
from __future__ import annotations

import csv
import json
import math
import statistics
from collections import defaultdict
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[2]
INPUT = ROOT / "data" / "history" / "historical_prediction_dataset.csv"
OUTPUT = ROOT / "data" / "analysis" / "current" / "rule_hypothesis_validation.json"


def number(row: dict[str, str], key: str) -> float | None:
    try:
        value = float(row.get(key, ""))
        return value if math.isfinite(value) else None
    except (TypeError, ValueError):
        return None


def sign(value: float | None, band: float = 0.0) -> int:
    if value is None or abs(value) <= band:
        return 0
    return 1 if value > 0 else -1


def same_sign(values: list[float | None], bands: list[float]) -> int:
    directions = [sign(value, band) for value, band in zip(values, bands)]
    return directions[0] if directions and directions[0] != 0 and len(set(directions)) == 1 else 0


def third_wednesday(value: date) -> bool:
    return value.weekday() == 2 and 15 <= value.day <= 21


def yearly_stats(samples: list[dict[str, Any]], directional: bool) -> dict[str, dict[str, Any]]:
    years: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in samples:
        years[item["target_date"][:4]].append(item)
    output = {}
    for year, items in sorted(years.items()):
        hits = [item["hit"] for item in items if item.get("hit") is not None]
        output[year] = {
            "sample_size": len(items),
            "next_day_up_rate": sum(item["return"] > 0 for item in items) / len(items),
            "avg_next_day_return": statistics.fmean(item["return"] for item in items),
            "directional_accuracy": statistics.fmean(hits) if directional and hits else None,
        }
    return output


def classify(sample_size: int, accuracy: float | None, up_rate: float, base_up_rate: float, yearly: dict[str, dict[str, Any]], directional: bool) -> str:
    if sample_size < 30:
        return "INSUFFICIENT_DATA"
    metric = accuracy if directional else 0.5 + abs(up_rate - base_up_rate)
    yearly_metrics = [value["directional_accuracy"] if directional else abs(value["next_day_up_rate"] - base_up_rate) + 0.5 for value in yearly.values() if value["sample_size"] >= 10]
    spread = max(yearly_metrics) - min(yearly_metrics) if len(yearly_metrics) >= 2 else 0
    if metric >= 0.55 and spread <= 0.15:
        return "SUPPORTED"
    if metric >= 0.52 and spread <= 0.2:
        return "WEAK"
    if spread > 0.15:
        return "REGIME_DEPENDENT"
    return "REJECTED"


def evaluate(rows: list[dict[str, str]], hypothesis_id: str, description: str, rule: Callable[[int, dict[str, str]], int], *, directional: bool = True) -> dict[str, Any]:
    samples = []
    for index, row in enumerate(rows):
        prediction = rule(index, row)
        actual_return = number(row, "next_taiex_return")
        if prediction == 0 or actual_return is None:
            continue
        samples.append({"target_date": row["target_date"], "return": actual_return, "prediction": prediction, "hit": prediction == sign(actual_return) if directional else None})
    returns = [item["return"] for item in samples]
    base_up_rate = sum(number(row, "next_taiex_return") > 0 for row in rows) / len(rows)
    up_rate = sum(value > 0 for value in returns) / len(returns) if returns else 0
    accuracy = statistics.fmean(item["hit"] for item in samples) if directional and samples else None
    yearly = yearly_stats(samples, directional)
    return {
        "hypothesis_id": hypothesis_id,
        "description": description,
        "forecast_statistic_scope": "next_trading_day_TAIEX",
        "sample_size": len(samples),
        "next_day_up_rate": up_rate if samples else None,
        "avg_next_day_return": statistics.fmean(returns) if returns else None,
        "median_next_day_return": statistics.median(returns) if returns else None,
        "directional_accuracy": accuracy,
        "yearly_stability": yearly,
        "status": classify(len(samples), accuracy, up_rate, base_up_rate, yearly, directional),
    }


def build_report(input_path: Path = INPUT) -> dict[str, Any]:
    with input_path.open("r", encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.DictReader(stream))
    if not rows:
        raise ValueError("Historical dataset is empty")
    rows.sort(key=lambda row: row["feature_date"])
    closes = [number(row, "taiex_close") for row in rows]
    turnovers = [number(row, "turnover") for row in rows]

    def moving_average(values: list[float | None], index: int, window: int) -> float | None:
        sample = values[max(0, index - window + 1): index + 1]
        return statistics.fmean(sample) if len(sample) == window and all(value is not None for value in sample) else None

    hypotheses = [
        evaluate(rows, "night_futures_direction", "夜盤方向對隔日 TAIEX", lambda _, row: sign(number(row, "night_futures_change"), 0.3)),
        evaluate(rows, "us_market_direction", "S&P 500 與 NASDAQ 同向對隔日 TAIEX", lambda _, row: same_sign([number(row, "sp500_change_percent"), number(row, "nasdaq_change_percent")], [0.15, 0.2])),
        evaluate(rows, "sox_tsm_adr_resonance", "SOX 與 TSM ADR 共振", lambda _, row: same_sign([number(row, "sox_change_percent"), number(row, "tsm_adr_change_percent")], [0.3, 0.3])),
        evaluate(rows, "foreign_cash_futures_resonance", "外資現貨與期貨部位共振", lambda _, row: same_sign([number(row, "foreign_cash_flow"), number(row, "foreign_futures_position")], [5_000_000_000, 5_000])),
        evaluate(rows, "taiex_tpex_relative_strength", "TAIEX 與 TPEx 相對強弱延續", lambda index, row: sign((number(row, "tpex_close") / number(rows[index - 1], "tpex_close") - 1) * 100 - number(row, "taiex_change_percent"), 0.2) if index > 0 and number(row, "tpex_close") and number(rows[index - 1], "tpex_close") and number(row, "taiex_change_percent") is not None else 0),
        evaluate(rows, "market_breadth", "市場廣度方向", lambda _, row: sign(((number(row, "advancing") or 0) - (number(row, "declining") or 0)) / ((number(row, "advancing") or 0) + (number(row, "declining") or 0)), 0.1) if (number(row, "advancing") or 0) + (number(row, "declining") or 0) else 0),
        evaluate(rows, "price_volume", "價量同向且成交額高於 20 日均量", lambda index, row: sign(number(row, "taiex_change_percent"), 0.2) if moving_average(turnovers, index, 20) and number(row, "turnover") > moving_average(turnovers, index, 20) else 0),
        evaluate(rows, "vix_risk_off", "VIX 上升視為 risk-off", lambda _, row: -sign(number(row, "vix_change_percent"), 2.0)),
        evaluate(rows, "ma20_bias", "TAIEX 相對 20 日均線乖離延續", lambda index, row: sign((number(row, "taiex_close") / moving_average(closes, index, 20) - 1) * 100, 1.0) if moving_average(closes, index, 20) and number(row, "taiex_close") else 0),
        evaluate(rows, "settlement_calendar", "第三個星期三結算與月底日曆效果", lambda _, row: 1 if third_wednesday(date.fromisoformat(row["feature_date"])) or date.fromisoformat(row["feature_date"]).day >= 28 else 0, directional=False),
    ]
    source_date_fields = [field for field in rows[0] if field.endswith("_trade_date")]
    leakage = []
    for row in rows:
        for field in source_date_fields:
            value = row.get(field)
            if value and value > row["target_date"]:
                leakage.append({"feature_date": row["feature_date"], "target_date": row["target_date"], "field": field, "source_date": value})
    return {
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "dataset": str(input_path.relative_to(ROOT)).replace("\\", "/"),
        "sample_size": len(rows),
        "date_range": {"first_feature_date": rows[0]["feature_date"], "last_target_date": rows[-1]["target_date"]},
        "separation_note": "Market State 規則是當期狀態評分；本報告的 Next-Day Forecast 僅為獨立歷史統計，不會自動調整正式權重。",
        "temporal_leakage": {"count": len(leakage), "items": leakage[:100]},
        "hypotheses": hypotheses,
    }


def main() -> int:
    report = build_report()
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    temporary = OUTPUT.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(OUTPUT)
    print(f"Hypothesis validation written | samples={report['sample_size']} | leakage={report['temporal_leakage']['count']} | output={OUTPUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
