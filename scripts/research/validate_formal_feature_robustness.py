"""Leave-one-feature-out robustness audit for the formal feature set."""

from __future__ import annotations

import csv
import json
import logging
import math
import os
import statistics
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from config import PROJECT_ROOT


LOGGER = logging.getLogger("formal_feature_robustness")
INPUT_PATH = PROJECT_ROOT / "data" / "history" / "historical_prediction_dataset.csv"
OUTPUT_PATH = PROJECT_ROOT / "data" / "analysis" / "archive" / "formal_feature_robustness.json"
INITIAL_TRAINING_SIZE = 250
NEAR_ZERO_THRESHOLD = 0.05
FORMAL_FEATURES = (
    "taiex_close", "taiex_change_percent", "tpex_close", "turnover",
    "advancing", "declining", "unchanged", "foreign_cash_flow",
    "foreign_futures_position", "night_futures_change",
    "tsm_adr_change_percent", "sox_change_percent", "sp500_change_percent",
    "nasdaq_change_percent", "vix_change_percent", "kospi_change_percent",
)
METRICS = ("accuracy", "f1", "roc_auc", "brier_score", "log_loss")


def validate_formal_feature_robustness() -> dict[str, Any]:
    rows = _load_rows()
    baseline = _walk_forward(rows, FORMAL_FEATURES, record_coefficients=True)
    coefficient_paths = baseline.pop("coefficient_paths")
    leave_one_out: dict[str, Any] = {}
    for feature in FORMAL_FEATURES:
        selected = tuple(item for item in FORMAL_FEATURES if item != feature)
        result = _walk_forward(rows, selected)
        result["delta_vs_full"] = _delta(result, baseline)
        result["all_primary_metrics_improved"] = _all_primary_improved(result, baseline)
        leave_one_out[feature] = result

    coefficient_stability = {
        feature: _coefficient_summary(coefficient_paths[feature])
        for feature in FORMAL_FEATURES
    }
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "version": "1.0",
        "dataset": "historical_prediction_dataset",
        "algorithm": "StandardScaler + LogisticRegression(max_iter=1000, random_state=42)",
        "validation_method": "expanding_window_one_step_ahead",
        "initial_training_size": INITIAL_TRAINING_SIZE,
        "total_dataset_samples": len(rows),
        "prediction_count_per_experiment": len(rows) - INITIAL_TRAINING_SIZE,
        "formal_features": list(FORMAL_FEATURES),
        "near_zero_definition": f"absolute standardized coefficient < {NEAR_ZERO_THRESHOLD}",
        "baseline_full_16": baseline,
        "leave_one_feature_out": leave_one_out,
        "coefficient_stability": coefficient_stability,
        "features_improving_all_primary_metrics_when_removed": [
            feature for feature, result in leave_one_out.items()
            if result["all_primary_metrics_improved"]
        ],
        "production_model_modified": False,
    }
    _validate_report(payload)
    _write_atomic(payload)
    return payload


def _load_rows() -> list[dict[str, Any]]:
    required = {"feature_date", "target_date", "target_direction", *FORMAL_FEATURES}
    with INPUT_PATH.open("r", encoding="utf-8-sig", newline="") as handle:
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
            values = {feature: float(source[feature]) for feature in FORMAL_FEATURES}
            target = int(source["target_direction"])
            if target not in (0, 1) or not all(math.isfinite(value) for value in values.values()):
                raise ValueError(f"Invalid target or feature at row {number}")
            rows.append({
                "feature_date": feature_date, "target_date": target_date,
                "target_direction": target, **values,
            })
            previous = feature_date
    if len(rows) <= INITIAL_TRAINING_SIZE:
        raise ValueError("Historical dataset is too short")
    return rows


def _walk_forward(
    rows: list[dict[str, Any]],
    features: Iterable[str],
    *,
    record_coefficients: bool = False,
) -> dict[str, Any]:
    from sklearn.linear_model import LogisticRegression
    from sklearn.metrics import accuracy_score, brier_score_loss, f1_score, log_loss, roc_auc_score
    from sklearn.pipeline import Pipeline
    from sklearn.preprocessing import StandardScaler

    selected = tuple(features)
    actual: list[int] = []
    predicted: list[int] = []
    probabilities: list[float] = []
    hits: list[bool] = []
    dates: list[str] = []
    coefficient_paths = {feature: [] for feature in selected}
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
        outcome = current["target_direction"]
        actual.append(outcome); predicted.append(prediction); probabilities.append(probability)
        hits.append(prediction == outcome); dates.append(current["target_date"])
        if record_coefficients:
            coefficients = pipeline.named_steps["classifier"].coef_[0]
            for position, feature in enumerate(selected):
                coefficient_paths[feature].append(float(coefficients[position]))
    result = {
        "feature_count": len(selected),
        "features": list(selected),
        "prediction_count": len(actual),
        "prediction_target_dates": dates,
        "accuracy": _clean(accuracy_score(actual, predicted)),
        "f1": _clean(f1_score(actual, predicted, zero_division=0)),
        "roc_auc": _clean(roc_auc_score(actual, probabilities)),
        "brier_score": _clean(brier_score_loss(actual, probabilities)),
        "log_loss": _clean(log_loss(actual, probabilities, labels=[0, 1])),
        "recent_50_accuracy": _clean(sum(hits[-50:]) / 50),
        "recent_100_accuracy": _clean(sum(hits[-100:]) / 100),
    }
    if record_coefficients:
        result["coefficient_paths"] = coefficient_paths
    return result


def _delta(candidate: dict[str, Any], baseline: dict[str, Any]) -> dict[str, float]:
    fields = (*METRICS, "recent_50_accuracy", "recent_100_accuracy")
    return {field: _clean(candidate[field] - baseline[field]) for field in fields}


def _all_primary_improved(candidate: dict[str, Any], baseline: dict[str, Any]) -> bool:
    return (
        candidate["accuracy"] > baseline["accuracy"]
        and candidate["f1"] > baseline["f1"]
        and candidate["roc_auc"] > baseline["roc_auc"]
        and candidate["brier_score"] < baseline["brier_score"]
        and candidate["log_loss"] < baseline["log_loss"]
        and candidate["recent_50_accuracy"] >= baseline["recent_50_accuracy"]
        and candidate["recent_100_accuracy"] >= baseline["recent_100_accuracy"]
    )


def _coefficient_summary(values: list[float]) -> dict[str, Any]:
    signs = [1 if value > 0 else -1 for value in values if value != 0]
    near_zero = sum(abs(value) < NEAR_ZERO_THRESHOLD for value in values)
    return {
        "sample_count": len(values),
        "median": _clean(statistics.median(values)),
        "mean": _clean(statistics.fmean(values)),
        "min": _clean(min(values)),
        "max": _clean(max(values)),
        "positive_count": sum(value > 0 for value in values),
        "negative_count": sum(value < 0 for value in values),
        "zero_count": sum(value == 0 for value in values),
        "sign_flip_count": sum(left != right for left, right in zip(signs, signs[1:])),
        "near_zero_count": near_zero,
        "near_zero_ratio": _clean(near_zero / len(values)),
    }


def _clean(value: float) -> float:
    result = float(value)
    if not math.isfinite(result): raise ValueError("Non-finite validation result")
    return round(result, 12)


def _validate_report(payload: dict[str, Any]) -> None:
    baseline = payload["baseline_full_16"]
    expected = payload["prediction_count_per_experiment"]
    dates = baseline["prediction_target_dates"]
    if len(dates) != expected or len(set(dates)) != expected:
        raise ValueError("Baseline OOS dates are invalid")
    if len(payload["leave_one_feature_out"]) != len(FORMAL_FEATURES):
        raise ValueError("Expected one ablation for every formal feature")
    for feature, result in payload["leave_one_feature_out"].items():
        if result["prediction_count"] != expected or result["prediction_target_dates"] != dates:
            raise ValueError(f"OOS dates differ when removing {feature}")
    for summary in payload["coefficient_stability"].values():
        if summary["sample_count"] != expected or not 0 <= summary["near_zero_ratio"] <= 1:
            raise ValueError("Coefficient summary is inconsistent")


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
    try:
        report = validate_formal_feature_robustness()
    except (OSError, RuntimeError, ValueError) as exc:
        LOGGER.error("Formal feature robustness failed: %s", exc)
        return 1
    LOGGER.info(
        "Formal feature robustness completed | experiments=%d | predictions=%d",
        len(report["leave_one_feature_out"]) + 1,
        report["prediction_count_per_experiment"],
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
