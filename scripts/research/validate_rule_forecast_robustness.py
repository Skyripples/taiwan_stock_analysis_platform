"""Leakage-safe robustness checks for the next-day rule forecast."""
from __future__ import annotations

import csv
import json
import math
import statistics
from collections import Counter, defaultdict
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
INPUT = ROOT / "data" / "history" / "historical_prediction_dataset.csv"
PHASE3_REPORT = ROOT / "data" / "analysis" / "current" / "next_day_rule_forecast_validation.json"
OUTPUT = ROOT / "data" / "analysis" / "current" / "next_day_rule_forecast_robustness.json"
INITIAL = 250
FIXED_FOUR = ("night_futures_direction", "us_market_direction", "sox_tsm_adr_resonance", "vix_risk_off")


def num(row: dict[str, Any], key: str) -> float | None:
    try:
        value = float(row.get(key, ""))
        return value if math.isfinite(value) else None
    except (TypeError, ValueError):
        return None


def sign(value: float | None, band: float = 0) -> int:
    return 0 if value is None or abs(value) <= band else (1 if value > 0 else -1)


def resonance(row: dict[str, Any], keys: tuple[str, str], bands: tuple[float, float]) -> int:
    values = [sign(num(row, key), band) for key, band in zip(keys, bands)]
    return values[0] if values[0] and values[0] == values[1] else 0


def moving_average(values: list[float | None], index: int, window: int) -> float | None:
    sample = values[index - window + 1:index + 1]
    return statistics.fmean(sample) if len(sample) == window and all(value is not None for value in sample) else None


def build_signals(rows: list[dict[str, Any]]) -> None:
    closes = [num(row, "taiex_close") for row in rows]
    turnovers = [num(row, "turnover") for row in rows]
    for index, row in enumerate(rows):
        tpex_previous = num(rows[index - 1], "tpex_close") if index else None
        tpex_current = num(row, "tpex_close")
        taiex_change = num(row, "taiex_change_percent")
        relative = (tpex_current / tpex_previous - 1) * 100 - taiex_change if tpex_current and tpex_previous and taiex_change is not None else None
        advancing, declining = num(row, "advancing"), num(row, "declining")
        breadth = (advancing - declining) / (advancing + declining) if advancing is not None and declining is not None and advancing + declining else None
        turnover_ma20 = moving_average(turnovers, index, 20)
        close_ma20 = moving_average(closes, index, 20)
        feature_day = date.fromisoformat(row["feature_date"])
        row["_signals"] = {
            "night_futures_direction": sign(num(row, "night_futures_change")),
            "us_market_direction": resonance(row, ("sp500_change_percent", "nasdaq_change_percent"), (0.15, 0.2)),
            "sox_tsm_adr_resonance": resonance(row, ("sox_change_percent", "tsm_adr_change_percent"), (0.3, 0.3)),
            "foreign_cash_futures_resonance": resonance(row, ("foreign_cash_flow", "foreign_futures_position"), (5_000_000_000, 5_000)),
            "taiex_tpex_relative_strength": sign(relative, 0.2),
            "market_breadth": sign(breadth, 0.1),
            "price_volume": sign(taiex_change, 0.2) if turnover_ma20 and num(row, "turnover") > turnover_ma20 else 0,
            "vix_risk_off": -sign(num(row, "vix_change_percent"), 2.0),
            "ma20_bias": sign((num(row, "taiex_close") / close_ma20 - 1) * 100, 1.0) if close_ma20 and num(row, "taiex_close") else 0,
            "settlement_calendar": 1 if (feature_day.weekday() == 2 and 15 <= feature_day.day <= 21) or feature_day.day >= 28 else 0,
        }


def quality(history: list[dict[str, Any]], key: str) -> dict[str, Any]:
    hits, yearly = [], defaultdict(list)
    for row in history:
        forecast, actual = row["_signals"][key], sign(num(row, "next_taiex_return"))
        if not forecast or not actual:
            continue
        hit = forecast == actual
        hits.append(hit)
        yearly[row["target_date"][:4]].append(hit)
    accuracy = statistics.fmean(hits) if hits else 0.5
    yearly_accuracy = [statistics.fmean(items) for items in yearly.values() if len(items) >= 10]
    spread = max(yearly_accuracy) - min(yearly_accuracy) if len(yearly_accuracy) >= 2 else 0
    stability = max(0.0, 1.0 - 2.0 * statistics.pstdev(yearly_accuracy)) if len(yearly_accuracy) >= 2 else 0.5
    supported = len(hits) >= 30 and accuracy >= 0.55 and spread <= 0.15
    return {"samples": len(hits), "accuracy": accuracy, "yearly_spread": spread, "stability": stability, "supported": supported, "raw_weight": max(0.0, accuracy - 0.5) * stability}


def weights_for(history: list[dict[str, Any]], keys: list[str], require_supported: bool) -> tuple[dict[str, float], dict[str, dict[str, Any]]]:
    details = {key: quality(history, key) for key in keys}
    selected = [key for key in keys if details[key]["supported"]] if require_supported else keys
    total = sum(details[key]["raw_weight"] for key in selected)
    weights = {key: details[key]["raw_weight"] / total * 100 for key in selected} if total else ({key: 100 / len(selected) for key in selected} if selected else {})
    return weights, details


def predict(row: dict[str, Any], weights: dict[str, float]) -> tuple[float, int]:
    active = {key: row["_signals"][key] for key in weights if row["_signals"][key]}
    denominator = sum(weights[key] for key in active)
    if denominator <= 0:
        return 50.0, 0
    score = 50 + 50 * sum(weights[key] * signal for key, signal in active.items()) / denominator
    return max(0.0, min(100.0, score)), sign(score - 50)


def summarize(items: list[dict[str, Any]]) -> dict[str, Any]:
    evaluated = [item for item in items if item["prediction"]]
    yearly = defaultdict(list)
    for item in items:
        yearly[item["target_date"][:4]].append(item)
    extreme = [item for item in evaluated if item["score"] <= 20 or item["score"] >= 80]
    return {
        "samples": len(items),
        "evaluated_samples": len(evaluated),
        "accuracy": statistics.fmean(item["hit"] for item in evaluated) if evaluated else None,
        "extreme_samples": len(extreme),
        "extreme_coverage": len(extreme) / len(items),
        "extreme_accuracy": statistics.fmean(item["hit"] for item in extreme) if extreme else None,
        "score_0_20": bucket(items, 0, 20),
        "score_80_100": bucket(items, 80, 100),
        "yearly_accuracy": {year: summarize_year(values) for year, values in sorted(yearly.items())},
    }


def bucket(items: list[dict[str, Any]], lower: float, upper: float) -> dict[str, Any]:
    selected = [item for item in items if lower <= item["score"] <= upper]
    return {"samples": len(selected), "accuracy": statistics.fmean(item["hit"] for item in selected if item["hit"] is not None) if any(item["hit"] is not None for item in selected) else None}


def summarize_year(items: list[dict[str, Any]]) -> dict[str, Any]:
    evaluated = [item for item in items if item["prediction"]]
    return {"samples": len(items), "accuracy": statistics.fmean(item["hit"] for item in evaluated) if evaluated else None}


def run_model(rows: list[dict[str, Any]], mode: str, fixed_weights: dict[str, float] | None = None) -> tuple[list[dict[str, Any]], Counter]:
    output, selections = [], Counter()
    all_keys = list(rows[0]["_signals"])
    for index in range(INITIAL, len(rows)):
        if fixed_weights is not None:
            weights = fixed_weights
        elif mode == "dynamic_selection":
            weights, _ = weights_for(rows[:index], all_keys, True)
        else:
            weights, _ = weights_for(rows[:index], list(FIXED_FOUR), False)
        selections.update(weights.keys())
        score, predicted = predict(rows[index], weights)
        actual = sign(num(rows[index], "next_taiex_return"))
        output.append({"feature_date": rows[index]["feature_date"], "target_date": rows[index]["target_date"], "score": score, "prediction": predicted, "actual": actual, "hit": predicted == actual if predicted and actual else None})
    return output, selections


def validate() -> dict[str, Any]:
    with INPUT.open("r", encoding="utf-8-sig", newline="") as stream:
        rows = sorted(list(csv.DictReader(stream)), key=lambda row: row["feature_date"])
    build_signals(rows)
    fixed_weights = json.loads(PHASE3_REPORT.read_text(encoding="utf-8"))["suggested_weights_from_all_history"]
    dynamic, selections = run_model(rows, "dynamic_selection")
    fixed_dynamic, _ = run_model(rows, "fixed_four_dynamic")
    fixed_reference, _ = run_model(rows, "fixed_four_fixed", fixed_weights)
    test_rows = rows[INITIAL:]
    always_up = statistics.fmean(sign(num(row, "next_taiex_return")) == 1 for row in test_rows)
    leakage = []
    for row in rows:
        for field in ("night_futures_trade_date", "sp500_trade_date", "nasdaq_trade_date", "sox_trade_date", "tsm_adr_trade_date", "vix_trade_date"):
            if row.get(field) and row[field] > row["target_date"]:
                leakage.append({"feature_date": row["feature_date"], "target_date": row["target_date"], "field": field, "source_date": row[field]})
    return {
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "method": {"type": "expanding_window", "initial_training_samples": INITIAL, "candidate_rules": list(rows[0]["_signals"]), "selection_rule": "training-only samples>=30, accuracy>=55%, yearly spread<=15pp", "same_date_or_future_used": False},
        "always_up_baseline": {"samples": len(test_rows), "accuracy": always_up},
        "models": {
            "dynamic_selection_dynamic_weights": {**summarize(dynamic), "selection_frequency": dict(selections)},
            "fixed_four_dynamic_weights": summarize(fixed_dynamic),
            "fixed_four_fixed_current_weights_reference": {**summarize(fixed_reference), "weights": fixed_weights, "caution": "Current suggested weights were derived from all 722 rows; this is a forward replay reference, not unbiased OOS weight selection."},
        },
        "temporal_leakage": {"count": len(leakage), "items": leakage[:100]},
    }


def main() -> int:
    report = validate()
    temporary = OUTPUT.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(OUTPUT)
    print(f"Robustness report written | OOS={report['always_up_baseline']['samples']} | leakage={report['temporal_leakage']['count']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
