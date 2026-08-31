"""Robustness validation for KOSPI as a candidate sixteenth feature."""

from __future__ import annotations

import csv
import json
import logging
import math
import os
import statistics
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from config import PROJECT_ROOT


LOGGER = logging.getLogger("kospi_robustness")
INPUT_PATH = PROJECT_ROOT / "data" / "history" / "historical_prediction_dataset.csv"
OUTPUT_PATH = PROJECT_ROOT / "data" / "analysis" / "archive" / "kospi_robustness_validation.json"
INITIAL_TRAINING_SIZE = 250
FORMAL_FEATURES = (
    "taiex_close", "taiex_change_percent", "tpex_close", "turnover",
    "advancing", "declining", "unchanged", "foreign_cash_flow",
    "foreign_futures_position", "night_futures_change",
    "tsm_adr_change_percent", "sox_change_percent", "sp500_change_percent",
    "nasdaq_change_percent", "vix_change_percent",
)
KOSPI_FEATURE = "kospi_change_percent"


def validate_kospi_robustness(
    input_path: Path = INPUT_PATH, output_path: Path = OUTPUT_PATH
) -> dict[str, Any]:
    rows = _load_rows(input_path)
    volatility = _rolling_volatility(rows, window=20)
    model_a = _walk_forward(rows, FORMAL_FEATURES)
    model_b = _walk_forward(rows, (*FORMAL_FEATURES, KOSPI_FEATURE), record_kospi=True)
    if [item["target_date"] for item in model_a["predictions"]] != [
        item["target_date"] for item in model_b["predictions"]
    ]:
        raise ValueError("A/B walk-forward prediction dates are inconsistent")

    oos_volatility = [volatility[index] for index in range(INITIAL_TRAINING_SIZE, len(rows))]
    if any(value is None for value in oos_volatility):
        raise ValueError("OOS volatility unexpectedly contains missing values")
    threshold = statistics.median(float(value) for value in oos_volatility if value is not None)
    regimes = {
        "taiex_up": lambda index: rows[index]["taiex_change_percent"] > 0,
        "taiex_down_or_flat": lambda index: rows[index]["taiex_change_percent"] <= 0,
        "high_volatility": lambda index: float(volatility[index]) > threshold,
        "low_volatility": lambda index: float(volatility[index]) <= threshold,
    }
    regime_results: dict[str, Any] = {}
    for name, selector in regimes.items():
        positions = [
            index - INITIAL_TRAINING_SIZE
            for index in range(INITIAL_TRAINING_SIZE, len(rows))
            if selector(index)
        ]
        a_metrics = _metrics([model_a["predictions"][position] for position in positions])
        b_metrics = _metrics([model_b["predictions"][position] for position in positions])
        regime_results[name] = {
            "sample_count": len(positions),
            "A_formal_15": a_metrics,
            "B_15_plus_kospi": b_metrics,
            "B_minus_A": _delta(b_metrics, a_metrics),
        }

    yearly_results: dict[str, Any] = {}
    years = sorted({item["target_date"][:4] for item in model_a["predictions"]})
    for year in years:
        a_items = [item for item in model_a["predictions"] if item["target_date"].startswith(year)]
        b_items = [item for item in model_b["predictions"] if item["target_date"].startswith(year)]
        a_metrics, b_metrics = _metrics(a_items), _metrics(b_items)
        yearly_results[year] = {
            "sample_count": len(a_items),
            "A_formal_15": a_metrics,
            "B_15_plus_kospi": b_metrics,
            "B_minus_A": _delta(b_metrics, a_metrics),
        }

    coefficients = model_b.pop("kospi_coefficients")
    coefficient_stats = {
        "sample_count": len(coefficients),
        "median": _clean(statistics.median(coefficients)),
        "min": _clean(min(coefficients)),
        "max": _clean(max(coefficients)),
        "mean": _clean(statistics.fmean(coefficients)),
        "positive_count": sum(value > 0 for value in coefficients),
        "negative_count": sum(value < 0 for value in coefficients),
        "zero_count": sum(value == 0 for value in coefficients),
        "sign_flip_count": _sign_flips(coefficients),
    }
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "version": "1.0",
        "dataset": "historical_prediction_dataset",
        "algorithm": "StandardScaler + LogisticRegression(max_iter=1000, random_state=42)",
        "validation_method": "expanding_window_one_step_ahead",
        "initial_training_size": INITIAL_TRAINING_SIZE,
        "total_samples": len(rows),
        "prediction_count": len(rows) - INITIAL_TRAINING_SIZE,
        "models": {"A_formal_15": model_a, "B_15_plus_kospi": model_b},
        "B_minus_A": _delta(model_b["overall"], model_a["overall"]),
        "yearly_comparison": yearly_results,
        "regime_definition": {
            "taiex_direction": "feature-date taiex_change_percent > 0 versus <= 0",
            "volatility": "population standard deviation of the trailing 20 feature-date TAIEX daily returns; current feature date included, target date excluded",
            "volatility_median_threshold": _clean(threshold),
            "threshold_usage": "descriptive OOS regime split only; never supplied to either model",
        },
        "regime_comparison": regime_results,
        "kospi_standardized_coefficient": coefficient_stats,
        "production_model_modified": False,
    }
    _validate_report(payload)
    _write_atomic(output_path, payload)
    return payload


def _load_rows(path: Path) -> list[dict[str, Any]]:
    required = {"feature_date", "target_date", "target_direction", *FORMAL_FEATURES, KOSPI_FEATURE}
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        missing = sorted(required.difference(reader.fieldnames or ()))
        if missing:
            raise ValueError("Historical dataset is missing columns: " + ", ".join(missing))
        rows: list[dict[str, Any]] = []
        previous = ""
        for number, source in enumerate(reader, 2):
            feature_date, target_date = source["feature_date"], source["target_date"]
            if target_date <= feature_date or (previous and feature_date <= previous):
                raise ValueError(f"Invalid chronological dates at row {number}")
            values = {feature: float(source[feature]) for feature in (*FORMAL_FEATURES, KOSPI_FEATURE)}
            target = int(source["target_direction"])
            if target not in (0, 1) or not all(math.isfinite(value) for value in values.values()):
                raise ValueError(f"Invalid target or feature at row {number}")
            rows.append({"feature_date": feature_date, "target_date": target_date, "target_direction": target, **values})
            previous = feature_date
    if len(rows) <= INITIAL_TRAINING_SIZE:
        raise ValueError("Historical dataset is too short")
    return rows


def _rolling_volatility(rows: list[dict[str, Any]], window: int) -> list[float | None]:
    daily_returns: list[float | None] = [None]
    for index in range(1, len(rows)):
        previous = rows[index - 1]["taiex_close"]
        daily_returns.append((rows[index]["taiex_close"] / previous - 1) * 100)
    result: list[float | None] = []
    for index in range(len(rows)):
        values = daily_returns[index - window + 1 : index + 1]
        result.append(
            statistics.pstdev(float(value) for value in values if value is not None)
            if index >= window and all(value is not None for value in values)
            else None
        )
    return result


def _walk_forward(rows: list[dict[str, Any]], features: Iterable[str], record_kospi: bool = False) -> dict[str, Any]:
    from sklearn.linear_model import LogisticRegression
    from sklearn.pipeline import Pipeline
    from sklearn.preprocessing import StandardScaler

    selected = tuple(features)
    predictions: list[dict[str, Any]] = []
    coefficients: list[float] = []
    for index in range(INITIAL_TRAINING_SIZE, len(rows)):
        training, current = rows[:index], rows[index]
        pipeline = Pipeline([
            ("scaler", StandardScaler()),
            ("classifier", LogisticRegression(max_iter=1000, random_state=42)),
        ])
        pipeline.fit(
            [[row[feature] for feature in selected] for row in training],
            [row["target_direction"] for row in training],
        )
        values = [[current[feature] for feature in selected]]
        prediction = int(pipeline.predict(values)[0])
        class_indexes = {int(label): position for position, label in enumerate(pipeline.classes_)}
        probability = float(pipeline.predict_proba(values)[0][class_indexes[1]])
        predictions.append({
            "feature_date": current["feature_date"], "target_date": current["target_date"],
            "actual": current["target_direction"], "prediction": prediction,
            "up_probability": probability, "hit": prediction == current["target_direction"],
        })
        if record_kospi:
            feature_index = selected.index(KOSPI_FEATURE)
            coefficients.append(float(pipeline.named_steps["classifier"].coef_[0][feature_index]))
    output = {
        "feature_count": len(selected), "features": list(selected),
        "overall": _metrics(predictions),
        "recent_50_accuracy": _accuracy(predictions[-50:]),
        "recent_100_accuracy": _accuracy(predictions[-100:]),
        "predictions": predictions,
    }
    if record_kospi:
        output["kospi_coefficients"] = coefficients
    return output


def _metrics(items: list[dict[str, Any]]) -> dict[str, Any]:
    from sklearn.metrics import accuracy_score, brier_score_loss, f1_score, log_loss, roc_auc_score

    actual = [item["actual"] for item in items]
    predicted = [item["prediction"] for item in items]
    probabilities = [item["up_probability"] for item in items]
    if not items:
        raise ValueError("Cannot calculate metrics for an empty group")
    return {
        "sample_count": len(items),
        "accuracy": _clean(accuracy_score(actual, predicted)),
        "f1": _clean(f1_score(actual, predicted, zero_division=0)),
        "roc_auc": _clean(roc_auc_score(actual, probabilities)) if len(set(actual)) == 2 else None,
        "brier_score": _clean(brier_score_loss(actual, probabilities)),
        "log_loss": _clean(log_loss(actual, probabilities, labels=[0, 1])),
    }


def _accuracy(items: list[dict[str, Any]]) -> float:
    return _clean(sum(item["hit"] for item in items) / len(items))


def _delta(candidate: dict[str, Any], reference: dict[str, Any]) -> dict[str, float | None]:
    fields = ("accuracy", "f1", "roc_auc", "brier_score", "log_loss")
    return {
        field: None if candidate[field] is None or reference[field] is None else _clean(candidate[field] - reference[field])
        for field in fields
    }


def _sign_flips(values: list[float]) -> int:
    signs = [1 if value > 0 else -1 for value in values if value != 0]
    return sum(left != right for left, right in zip(signs, signs[1:]))


def _clean(value: float) -> float:
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("Non-finite validation result")
    return round(result, 12)


def _validate_report(payload: dict[str, Any]) -> None:
    expected = payload["prediction_count"]
    for model in payload["models"].values():
        if len(model["predictions"]) != expected:
            raise ValueError("Prediction counts differ")
        if any(not 0 <= item["up_probability"] <= 1 for item in model["predictions"]):
            raise ValueError("Probability outside [0, 1]")
    if sum(item["sample_count"] for item in payload["yearly_comparison"].values()) != expected:
        raise ValueError("Year groups do not cover all OOS predictions")
    regimes = payload["regime_comparison"]
    if regimes["taiex_up"]["sample_count"] + regimes["taiex_down_or_flat"]["sample_count"] != expected:
        raise ValueError("TAIEX direction regimes do not cover all OOS predictions")
    if regimes["high_volatility"]["sample_count"] + regimes["low_volatility"]["sample_count"] != expected:
        raise ValueError("Volatility regimes do not cover all OOS predictions")


def _write_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, allow_nan=False)
            handle.write("\n"); handle.flush(); os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists(): temporary.unlink()


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(message)s")
    try:
        report = validate_kospi_robustness()
    except (OSError, RuntimeError, ValueError) as exc:
        LOGGER.error("KOSPI robustness validation failed: %s", exc)
        return 1
    LOGGER.info("KOSPI robustness validation completed | predictions=%d", report["prediction_count"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
