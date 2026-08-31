"""Walk-forward and calibration validation for formal feature pruning candidates."""

from __future__ import annotations

import csv
import json
import logging
import math
import os
import statistics
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from config import PROJECT_ROOT


LOGGER = logging.getLogger("feature_pruning")
INPUT_PATH = PROJECT_ROOT / "data" / "history" / "historical_prediction_dataset.csv"
OUTPUT_PATH = PROJECT_ROOT / "data" / "analysis" / "archive" / "feature_pruning_validation.json"
INITIAL_TRAINING_SIZE = 250
CALIBRATION_FRACTION = 0.7
NEAR_ZERO_THRESHOLD = 0.05
FORMAL_FEATURES = (
    "taiex_close", "taiex_change_percent", "tpex_close", "turnover",
    "advancing", "declining", "unchanged", "foreign_cash_flow",
    "foreign_futures_position", "night_futures_change",
    "tsm_adr_change_percent", "sox_change_percent", "sp500_change_percent",
    "nasdaq_change_percent", "vix_change_percent", "kospi_change_percent",
)
PRUNE_ORDER = ("tsm_adr_change_percent", "taiex_change_percent", "unchanged")


def validate_feature_pruning() -> dict[str, Any]:
    rows = _load_rows()
    feature_sets = {
        "A_formal_16": FORMAL_FEATURES,
        "B_remove_tsm_adr": _without("tsm_adr_change_percent"),
        "C_remove_taiex_change": _without("taiex_change_percent"),
        "D_remove_unchanged": _without("unchanged"),
        "E_remove_all_three": _without(*PRUNE_ORDER),
        "F2_remove_tsm_then_taiex_change": _without(*PRUNE_ORDER[:2]),
    }
    raw_results = {
        name: _walk_forward(rows, features)
        for name, features in feature_sets.items()
    }
    dates = [item["target_date"] for item in raw_results["A_formal_16"]["predictions"]]
    volatility = _rolling_volatility(rows)
    threshold = statistics.median(volatility[INITIAL_TRAINING_SIZE:])
    models = {
        name: _summarize(result, rows, volatility, threshold)
        for name, result in raw_results.items()
    }
    baseline = models["A_formal_16"]
    for name, result in models.items():
        if name != "A_formal_16":
            result["delta_vs_A"] = _delta(result["overall"], baseline["overall"])
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "version": "1.0",
        "dataset": "historical_prediction_dataset",
        "algorithm": "StandardScaler + LogisticRegression(max_iter=1000, random_state=42)",
        "validation_method": "expanding_window_one_step_ahead",
        "initial_training_size": INITIAL_TRAINING_SIZE,
        "total_samples": len(rows),
        "prediction_count_per_experiment": len(dates),
        "oos_target_start": dates[0],
        "oos_target_end": dates[-1],
        "volatility_regime": {
            "definition": "feature-date trailing 20-session TAIEX return population volatility",
            "median_threshold": _clean(threshold),
            "used_for_model": False,
        },
        "calibration": {
            "method": "platt_logistic_on_raw_probability_logit",
            "split": "chronological_70_30",
            "fit_samples": int(len(dates) * CALIBRATION_FRACTION),
            "test_samples": len(dates) - int(len(dates) * CALIBRATION_FRACTION),
        },
        "models": models,
        "progressive_pruning": {
            "F1_remove_tsm_adr": "same result as B_remove_tsm_adr",
            "F2_remove_tsm_then_taiex_change": "models.F2_remove_tsm_then_taiex_change",
            "F3_remove_tsm_then_taiex_change_then_unchanged": "same result as E_remove_all_three",
        },
        "near_zero_definition": f"absolute standardized coefficient < {NEAR_ZERO_THRESHOLD}",
        "production_model_modified": False,
    }
    _validate(payload, raw_results)
    _write_atomic(payload)
    return payload


def _without(*removed: str) -> tuple[str, ...]:
    return tuple(feature for feature in FORMAL_FEATURES if feature not in set(removed))


def _load_rows() -> list[dict[str, Any]]:
    required = {"feature_date", "target_date", "target_direction", *FORMAL_FEATURES}
    with INPUT_PATH.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        missing = sorted(required.difference(reader.fieldnames or ()))
        if missing: raise ValueError("Historical dataset is missing columns: " + ", ".join(missing))
        rows: list[dict[str, Any]] = []
        previous = ""
        for number, source in enumerate(reader, 2):
            if source["target_date"] <= source["feature_date"] or (previous and source["feature_date"] <= previous):
                raise ValueError(f"Invalid chronological dates at row {number}")
            values = {feature: float(source[feature]) for feature in FORMAL_FEATURES}
            target = int(source["target_direction"])
            if target not in (0, 1) or not all(math.isfinite(value) for value in values.values()):
                raise ValueError(f"Invalid value at row {number}")
            rows.append({"feature_date": source["feature_date"], "target_date": source["target_date"], "target_direction": target, **values})
            previous = source["feature_date"]
    return rows


def _walk_forward(rows: list[dict[str, Any]], features: tuple[str, ...]) -> dict[str, Any]:
    from sklearn.linear_model import LogisticRegression
    from sklearn.pipeline import Pipeline
    from sklearn.preprocessing import StandardScaler

    predictions: list[dict[str, Any]] = []
    coefficients = {feature: [] for feature in features}
    for index in range(INITIAL_TRAINING_SIZE, len(rows)):
        training, current = rows[:index], rows[index]
        pipeline = Pipeline([("scaler", StandardScaler()), ("classifier", LogisticRegression(max_iter=1000, random_state=42))])
        pipeline.fit([[row[feature] for feature in features] for row in training], [row["target_direction"] for row in training])
        values = [[current[feature] for feature in features]]
        probability = float(pipeline.predict_proba(values)[0][list(pipeline.classes_).index(1)])
        prediction = int(probability >= 0.5)
        predictions.append({
            "feature_date": current["feature_date"], "target_date": current["target_date"],
            "actual": current["target_direction"], "prediction": prediction,
            "up_probability": probability, "hit": prediction == current["target_direction"],
            "source_index": index,
        })
        for position, feature in enumerate(features):
            coefficients[feature].append(float(pipeline.named_steps["classifier"].coef_[0][position]))
    return {"features": list(features), "predictions": predictions, "coefficients": coefficients}


def _summarize(result: dict[str, Any], rows: list[dict[str, Any]], volatility: list[float], threshold: float) -> dict[str, Any]:
    predictions = result["predictions"]
    yearly = {
        year: _metrics([item for item in predictions if item["target_date"].startswith(year)])
        for year in ("2024", "2025", "2026")
    }
    regimes = {
        "high_volatility": _metrics([item for item in predictions if volatility[item["source_index"]] > threshold]),
        "low_volatility": _metrics([item for item in predictions if volatility[item["source_index"]] <= threshold]),
    }
    return {
        "feature_count": len(result["features"]),
        "features": result["features"],
        "overall": {**_metrics(predictions), "recent_50_accuracy": _accuracy(predictions[-50:]), "recent_100_accuracy": _accuracy(predictions[-100:])},
        "yearly": yearly,
        "volatility_regimes": regimes,
        "platt_calibration": _platt(predictions),
        "coefficient_stability": {feature: _coefficient_summary(values) for feature, values in result["coefficients"].items()},
    }


def _metrics(items: list[dict[str, Any]]) -> dict[str, Any]:
    from sklearn.metrics import accuracy_score, brier_score_loss, f1_score, log_loss, precision_score, recall_score, roc_auc_score

    actual, predicted = [item["actual"] for item in items], [item["prediction"] for item in items]
    probabilities = [item["up_probability"] for item in items]
    return {
        "sample_count": len(items),
        "accuracy": _clean(accuracy_score(actual, predicted)),
        "precision": _clean(precision_score(actual, predicted, zero_division=0)),
        "recall": _clean(recall_score(actual, predicted, zero_division=0)),
        "f1": _clean(f1_score(actual, predicted, zero_division=0)),
        "roc_auc": _clean(roc_auc_score(actual, probabilities)),
        "brier_score": _clean(brier_score_loss(actual, probabilities)),
        "log_loss": _clean(log_loss(actual, probabilities, labels=[0, 1])),
    }


def _platt(predictions: list[dict[str, Any]]) -> dict[str, Any]:
    from sklearn.linear_model import LogisticRegression

    split = int(len(predictions) * CALIBRATION_FRACTION)
    fit, test = predictions[:split], predictions[split:]
    calibrator = LogisticRegression(max_iter=1000, random_state=42)
    calibrator.fit([[_logit(item["up_probability"])] for item in fit], [item["actual"] for item in fit])
    indexes = {int(label): position for position, label in enumerate(calibrator.classes_)}
    calibrated = [float(values[indexes[1]]) for values in calibrator.predict_proba([[_logit(item["up_probability"])] for item in test])]
    targets = [item["actual"] for item in test]
    return {
        "fit_samples": len(fit), "test_samples": len(test),
        "fit_target_start": fit[0]["target_date"], "fit_target_end": fit[-1]["target_date"],
        "test_target_start": test[0]["target_date"], "test_target_end": test[-1]["target_date"],
        "brier_score": _clean(sum((value - target) ** 2 for value, target in zip(calibrated, targets)) / len(targets)),
        "log_loss": _log_loss(calibrated, targets),
        "expected_calibration_error": _ece(calibrated, targets),
    }


def _rolling_volatility(rows: list[dict[str, Any]]) -> list[float]:
    returns = [0.0] + [(rows[index]["taiex_close"] / rows[index - 1]["taiex_close"] - 1) * 100 for index in range(1, len(rows))]
    return [statistics.pstdev(returns[max(1, index - 19):index + 1]) if index >= 20 else 0.0 for index in range(len(rows))]


def _coefficient_summary(values: list[float]) -> dict[str, Any]:
    signs = [1 if value > 0 else -1 for value in values if value != 0]
    near_zero = sum(abs(value) < NEAR_ZERO_THRESHOLD for value in values)
    return {
        "median": _clean(statistics.median(values)), "mean": _clean(statistics.fmean(values)),
        "min": _clean(min(values)), "max": _clean(max(values)),
        "sign_flip_count": sum(left != right for left, right in zip(signs, signs[1:])),
        "near_zero_ratio": _clean(near_zero / len(values)),
    }


def _delta(candidate: dict[str, Any], baseline: dict[str, Any]) -> dict[str, float]:
    fields = ("accuracy", "precision", "recall", "f1", "roc_auc", "brier_score", "log_loss", "recent_50_accuracy", "recent_100_accuracy")
    return {field: _clean(candidate[field] - baseline[field]) for field in fields}


def _accuracy(items: list[dict[str, Any]]) -> float: return _clean(sum(item["hit"] for item in items) / len(items))


def _logit(value: float) -> float:
    clipped = min(1 - 1e-15, max(1e-15, value)); return math.log(clipped / (1 - clipped))


def _log_loss(probabilities: list[float], targets: list[int]) -> float:
    from sklearn.metrics import log_loss
    return _clean(log_loss(targets, probabilities, labels=[0, 1]))


def _ece(probabilities: list[float], targets: list[int]) -> float:
    buckets = [[] for _ in range(10)]
    for index, value in enumerate(probabilities): buckets[min(int(value * 10), 9)].append(index)
    return _clean(sum(len(bucket) / len(targets) * abs(sum(probabilities[i] for i in bucket) / len(bucket) - sum(targets[i] for i in bucket) / len(bucket)) for bucket in buckets if bucket))


def _clean(value: float) -> float:
    result = float(value)
    if not math.isfinite(result): raise ValueError("Non-finite validation result")
    return round(result, 12)


def _validate(payload: dict[str, Any], raw: dict[str, Any]) -> None:
    expected = payload["prediction_count_per_experiment"]
    baseline_dates = [item["target_date"] for item in raw["A_formal_16"]["predictions"]]
    for name, result in raw.items():
        dates = [item["target_date"] for item in result["predictions"]]
        if len(dates) != expected or dates != baseline_dates: raise ValueError(f"OOS dates differ for {name}")
    models = payload["models"]
    for result in models.values():
        if sum(item["sample_count"] for item in result["yearly"].values()) != expected: raise ValueError("Yearly groups are incomplete")
        if sum(item["sample_count"] for item in result["volatility_regimes"].values()) != expected: raise ValueError("Regime groups are incomplete")
        if result["platt_calibration"]["fit_samples"] + result["platt_calibration"]["test_samples"] != expected: raise ValueError("Calibration split is incomplete")


def _write_atomic(payload: dict[str, Any]) -> None:
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = OUTPUT_PATH.with_name(f".{OUTPUT_PATH.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, allow_nan=False)
            handle.write("\n"); handle.flush(); os.fsync(handle.fileno())
        os.replace(temporary, OUTPUT_PATH)
    finally:
        if temporary.exists(): temporary.unlink()


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(message)s")
    try: report = validate_feature_pruning()
    except (OSError, RuntimeError, ValueError) as exc:
        LOGGER.error("Feature pruning validation failed: %s", exc); return 1
    LOGGER.info("Feature pruning validation completed | models=%d | predictions=%d", len(report["models"]), report["prediction_count_per_experiment"])
    return 0


if __name__ == "__main__": raise SystemExit(main())
