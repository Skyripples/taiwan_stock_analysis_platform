"""Walk-forward validation for the supported next-day rule forecast."""
from __future__ import annotations

import csv
import json
import math
import statistics
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[2]
INPUT = ROOT / "data" / "history" / "historical_prediction_dataset.csv"
OUTPUT = ROOT / "data" / "analysis" / "current" / "next_day_rule_forecast_validation.json"
INITIAL_TRAINING = 250


def number(row: dict[str, str], key: str) -> float | None:
    try:
        value = float(row.get(key, ""))
        return value if math.isfinite(value) else None
    except (TypeError, ValueError):
        return None


def direction(value: float | None, neutral_band: float = 0) -> int:
    if value is None or abs(value) <= neutral_band:
        return 0
    return 1 if value > 0 else -1


def resonance(row: dict[str, str], keys: tuple[str, str], bands: tuple[float, float]) -> int:
    signals = [direction(number(row, key), band) for key, band in zip(keys, bands)]
    return signals[0] if signals[0] != 0 and signals[0] == signals[1] else 0


RULES: dict[str, Callable[[dict[str, str]], int]] = {
    "night_futures_direction": lambda row: direction(number(row, "night_futures_change")),
    "us_market_direction": lambda row: resonance(row, ("sp500_change_percent", "nasdaq_change_percent"), (0.15, 0.2)),
    "sox_tsm_adr_resonance": lambda row: resonance(row, ("sox_change_percent", "tsm_adr_change_percent"), (0.3, 0.3)),
    "vix_risk_off": lambda row: -direction(number(row, "vix_change_percent"), 2.0),
}


def rule_quality(history: list[dict[str, str]], rule: Callable[[dict[str, str]], int]) -> dict[str, float]:
    hits: list[bool] = []
    yearly: dict[str, list[bool]] = defaultdict(list)
    for row in history:
        forecast, actual = rule(row), direction(number(row, "next_taiex_return"))
        if forecast == 0 or actual == 0:
            continue
        hit = forecast == actual
        hits.append(hit)
        yearly[row["target_date"][:4]].append(hit)
    accuracy = statistics.fmean(hits) if hits else 0.5
    yearly_accuracy = [statistics.fmean(items) for items in yearly.values() if len(items) >= 10]
    stability = max(0.0, 1.0 - 2.0 * statistics.pstdev(yearly_accuracy)) if len(yearly_accuracy) >= 2 else 0.5
    raw_weight = max(0.0, accuracy - 0.5) * stability
    return {"accuracy": accuracy, "stability": stability, "raw_weight": raw_weight, "samples": len(hits)}


def learned_weights(history: list[dict[str, str]]) -> tuple[dict[str, float], dict[str, dict[str, float]]]:
    quality = {key: rule_quality(history, rule) for key, rule in RULES.items()}
    total = sum(item["raw_weight"] for item in quality.values())
    weights = ({key: item["raw_weight"] / total * 100 for key, item in quality.items()} if total > 0 else {key: 25.0 for key in RULES})
    return weights, quality


def forecast(row: dict[str, str], weights: dict[str, float]) -> tuple[float, int, dict[str, int]]:
    signals = {key: rule(row) for key, rule in RULES.items()}
    active = {key: value for key, value in signals.items() if value != 0}
    active_weight = sum(weights[key] for key in active)
    if active_weight <= 0:
        return 50.0, 0, signals
    vote = sum(weights[key] * value for key, value in active.items()) / active_weight
    score = max(0.0, min(100.0, 50.0 + 50.0 * vote))
    return score, direction(score - 50), signals


def metric_block(items: list[dict[str, Any]]) -> dict[str, Any]:
    directional = [item for item in items if item["prediction"] != 0]
    return {
        "sample_size": len(items),
        "directional_samples": len(directional),
        "neutral_samples": len(items) - len(directional),
        "accuracy": statistics.fmean(item["hit"] for item in directional) if directional else None,
    }


def validate(input_path: Path = INPUT) -> dict[str, Any]:
    with input_path.open("r", encoding="utf-8-sig", newline="") as stream:
        rows = sorted(csv.DictReader(stream), key=lambda row: row["feature_date"])
    if len(rows) <= INITIAL_TRAINING:
        raise ValueError("Insufficient rows for walk-forward validation")
    predictions = []
    weight_history: dict[str, list[float]] = defaultdict(list)
    for index in range(INITIAL_TRAINING, len(rows)):
        weights, _ = learned_weights(rows[:index])
        score, predicted, signals = forecast(rows[index], weights)
        actual = direction(number(rows[index], "next_taiex_return"))
        predictions.append({"feature_date": rows[index]["feature_date"], "target_date": rows[index]["target_date"], "score": score, "prediction": predicted, "actual": actual, "hit": predicted == actual if predicted else None, "signals": signals})
        for key, value in weights.items():
            weight_history[key].append(value)

    yearly: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in predictions:
        yearly[item["target_date"][:4]].append(item)
    buckets = [(0, 20), (20, 40), (40, 60), (60, 80), (80, 100)]
    bucket_results = {}
    for lower, upper in buckets:
        selected = [item for item in predictions if lower <= item["score"] <= upper] if upper == 100 else [item for item in predictions if lower <= item["score"] < upper]
        bucket_results[f"{lower}-{upper}"] = metric_block(selected)

    final_weights, final_quality = learned_weights(rows)
    leakage = []
    checked_dates = ("night_futures_trade_date", "sp500_trade_date", "nasdaq_trade_date", "sox_trade_date", "tsm_adr_trade_date", "vix_trade_date")
    for row in rows:
        for field in checked_dates:
            if row.get(field) and row[field] > row["target_date"]:
                leakage.append({"feature_date": row["feature_date"], "target_date": row["target_date"], "field": field, "source_date": row[field]})
    always_up_accuracy = statistics.fmean(item["actual"] == 1 for item in predictions)
    return {
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "method": {"type": "expanding_window", "initial_training_samples": INITIAL_TRAINING, "weight_formula": "max(rule_accuracy - 0.5, 0) × max(0, 1 - 2 × yearly_accuracy_std); normalize active rules to 100", "same_row_weight_fitting": False},
        "separation_note": "This is a Next-Day Rule Forecast validation, not the current Market State score and not a probability.",
        "oos": metric_block(predictions),
        "always_up_baseline": {"sample_size": len(predictions), "accuracy": always_up_accuracy},
        "yearly_accuracy": {year: metric_block(items) for year, items in sorted(yearly.items())},
        "score_buckets": bucket_results,
        "average_walk_forward_weights": {key: statistics.fmean(values) for key, values in weight_history.items()},
        "suggested_weights_from_all_history": final_weights,
        "full_history_rule_quality": final_quality,
        "temporal_leakage": {"count": len(leakage), "items": leakage[:100]},
    }


def main() -> int:
    report = validate()
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    temporary = OUTPUT.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(OUTPUT)
    print(f"Next-day rule forecast validation written | oos={report['oos']['sample_size']} | leakage={report['temporal_leakage']['count']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
