"""Walk-forward validation for candidate global macro features."""

from __future__ import annotations

import csv
import json
import logging
import math
import os
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from config import PROJECT_ROOT


LOGGER = logging.getLogger("global_macro_validation")
INPUT_PATH = PROJECT_ROOT / "data" / "history" / "historical_prediction_dataset.csv"
OUTPUT_PATH = PROJECT_ROOT / "data" / "analysis" / "archive" / "global_macro_validation.json"
INITIAL_TRAINING_SIZE = 250
ORIGINAL_FEATURES = (
    "taiex_close", "taiex_change_percent", "tpex_close", "turnover",
    "advancing", "declining", "unchanged", "foreign_cash_flow",
    "foreign_futures_position", "night_futures_change",
    "tsm_adr_change_percent", "sox_change_percent",
    "sp500_change_percent", "nasdaq_change_percent",
)
MACRO_FEATURES = (
    "vix_change_percent", "us10y_change", "us2y_change",
    "yield_curve_10y_2y", "usdtwd_change_percent", "dxy_change_percent",
)
METRIC_NAMES = (
    "accuracy", "precision", "recall", "f1", "roc_auc", "brier_score", "log_loss",
)


def validate_global_macro_features(
    input_path: Path = INPUT_PATH,
    output_path: Path = OUTPUT_PATH,
) -> dict[str, Any]:
    rows = _load_rows(input_path)
    feature_sets = {
        "A_original_14": ORIGINAL_FEATURES,
        "B_original_plus_all_macro": (*ORIGINAL_FEATURES, *MACRO_FEATURES),
        "C_original_plus_vix": (*ORIGINAL_FEATURES, "vix_change_percent"),
        "D_original_plus_vix_dxy_us10y": (
            *ORIGINAL_FEATURES, "vix_change_percent", "dxy_change_percent", "us10y_change",
        ),
    }
    models = {name: _walk_forward(rows, features) for name, features in feature_sets.items()}
    baseline = models["A_original_14"]
    all_macro = models["B_original_plus_all_macro"]

    single_addition = {}
    leave_one_out = {}
    for macro in MACRO_FEATURES:
        single = _walk_forward(rows, (*ORIGINAL_FEATURES, macro))
        single_addition[macro] = {
            **single,
            "delta_vs_A": _metric_delta(single, baseline),
        }
        selected = tuple(feature for feature in (*ORIGINAL_FEATURES, *MACRO_FEATURES) if feature != macro)
        removed = _walk_forward(rows, selected)
        leave_one_out[macro] = {
            **removed,
            "delta_vs_B": _metric_delta(removed, all_macro),
        }

    payload: dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "version": "1.0",
        "dataset": "historical_prediction_dataset",
        "algorithm": "StandardScaler + LogisticRegression(max_iter=1000, random_state=42)",
        "validation_method": "expanding_window_one_step_ahead",
        "initial_training_size": INITIAL_TRAINING_SIZE,
        "total_dataset_samples": len(rows),
        "prediction_count_per_experiment": len(rows) - INITIAL_TRAINING_SIZE,
        "prediction_date_range": {
            "feature_start": rows[INITIAL_TRAINING_SIZE]["feature_date"],
            "feature_end": rows[-1]["feature_date"],
            "target_start": rows[INITIAL_TRAINING_SIZE]["target_date"],
            "target_end": rows[-1]["target_date"],
        },
        "models": models,
        "B_vs_A_delta": _metric_delta(all_macro, baseline),
        "C_vs_A_delta": _metric_delta(models["C_original_plus_vix"], baseline),
        "D_vs_A_delta": _metric_delta(models["D_original_plus_vix_dxy_us10y"], baseline),
        "single_macro_addition": single_addition,
        "macro_leave_one_out_from_B": leave_one_out,
        "selection_warning": "Results are descriptive walk-forward evidence; no production feature selection or model update was performed.",
    }
    _validate_report(payload)
    _write_json_atomic(output_path, payload)
    return payload


def _load_rows(path: Path) -> list[dict[str, Any]]:
    required = {"feature_date", "target_date", "target_direction", *ORIGINAL_FEATURES, *MACRO_FEATURES}
    try:
        handle = path.open("r", encoding="utf-8-sig", newline="")
    except FileNotFoundError as exc:
        raise ValueError(f"Historical prediction dataset not found: {path}") from exc
    rows = []
    with handle:
        reader = csv.DictReader(handle)
        missing = sorted(required.difference(reader.fieldnames or ()))
        if missing:
            raise ValueError("Historical dataset is missing columns: " + ", ".join(missing))
        previous = ""
        for number, source in enumerate(reader, start=2):
            feature_date, target_date = source["feature_date"], source["target_date"]
            if not feature_date or target_date <= feature_date or (previous and feature_date <= previous):
                raise ValueError(f"Invalid chronological dates at row {number}")
            try:
                target = int(source["target_direction"])
                values = {feature: float(source[feature]) for feature in (*ORIGINAL_FEATURES, *MACRO_FEATURES)}
            except (TypeError, ValueError) as exc:
                raise ValueError(f"Invalid numeric value at row {number}") from exc
            if target not in (0, 1) or not all(math.isfinite(value) for value in values.values()):
                raise ValueError(f"Invalid target or non-finite feature at row {number}")
            rows.append({"feature_date": feature_date, "target_date": target_date, "target_direction": target, **values})
            previous = feature_date
    if len(rows) <= INITIAL_TRAINING_SIZE:
        raise ValueError("Historical dataset is too short for walk-forward validation")
    return rows


def _walk_forward(rows: list[dict[str, Any]], features: Iterable[str]) -> dict[str, Any]:
    try:
        from sklearn.linear_model import LogisticRegression
        from sklearn.metrics import (
            accuracy_score, brier_score_loss, f1_score, log_loss,
            precision_score, recall_score, roc_auc_score,
        )
        from sklearn.pipeline import Pipeline
        from sklearn.preprocessing import StandardScaler
    except ModuleNotFoundError as exc:
        raise RuntimeError("scikit-learn is required; run: pip install -r requirements.txt") from exc
    selected = tuple(features)
    actual: list[int] = []
    predicted: list[int] = []
    probabilities: list[float] = []
    dates: list[str] = []
    hits: list[bool] = []
    for index in range(INITIAL_TRAINING_SIZE, len(rows)):
        training = rows[:index]
        current = rows[index]
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
        indexes = {int(label): position for position, label in enumerate(pipeline.classes_)}
        probability = float(pipeline.predict_proba(values)[0][indexes[1]])
        outcome = current["target_direction"]
        actual.append(outcome); predicted.append(prediction); probabilities.append(probability)
        dates.append(current["target_date"]); hits.append(prediction == outcome)

    yearly: dict[str, list[bool]] = defaultdict(list)
    for target_date, hit in zip(dates, hits):
        yearly[target_date[:4]].append(hit)
    return {
        "feature_count": len(selected),
        "features": list(selected),
        "prediction_count": len(actual),
        "accuracy": _clean(accuracy_score(actual, predicted)),
        "precision": _clean(precision_score(actual, predicted, zero_division=0)),
        "recall": _clean(recall_score(actual, predicted, zero_division=0)),
        "f1": _clean(f1_score(actual, predicted, zero_division=0)),
        "roc_auc": _clean(roc_auc_score(actual, probabilities)),
        "brier_score": _clean(brier_score_loss(actual, probabilities)),
        "log_loss": _clean(log_loss(actual, probabilities, labels=[0, 1])),
        "recent_50_accuracy": _clean(sum(hits[-50:]) / 50),
        "recent_100_accuracy": _clean(sum(hits[-100:]) / 100),
        "yearly_accuracy": {
            year: {"sample_count": len(year_hits), "accuracy": _clean(sum(year_hits) / len(year_hits))}
            for year, year_hits in sorted(yearly.items())
        },
    }


def _metric_delta(candidate: dict[str, Any], reference: dict[str, Any]) -> dict[str, float]:
    return {metric: _clean(candidate[metric] - reference[metric]) for metric in METRIC_NAMES}


def _clean(value: float) -> float:
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("Global macro validation metric is non-finite")
    return round(result, 12)


def _validate_report(payload: dict[str, Any]) -> None:
    expected = payload["prediction_count_per_experiment"]
    results = [
        *payload["models"].values(),
        *payload["single_macro_addition"].values(),
        *payload["macro_leave_one_out_from_B"].values(),
    ]
    if len(results) != 16:
        raise ValueError("Expected exactly 16 macro experiments")
    for result in results:
        if result["prediction_count"] != expected:
            raise ValueError("Walk-forward prediction dates are inconsistent")
        if sum(item["sample_count"] for item in result["yearly_accuracy"].values()) != expected:
            raise ValueError("Yearly sample counts are inconsistent")
        for metric in METRIC_NAMES:
            if not 0 <= result[metric] <= 1:
                raise ValueError(f"Metric outside [0, 1]: {metric}")


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as output:
            json.dump(payload, output, ensure_ascii=False, indent=2, allow_nan=False)
            output.write("\n"); output.flush(); os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists(): temporary.unlink()


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(message)s")
    LOGGER.info("Global macro walk-forward validation started")
    try:
        report = validate_global_macro_features()
    except (OSError, RuntimeError, ValueError) as exc:
        LOGGER.error("Global macro validation failed: %s", exc)
        return 1
    LOGGER.info("Global macro validation written: %s", OUTPUT_PATH)
    LOGGER.info("Global macro validation finished | experiments=16 | predictions_each=%d", report["prediction_count_per_experiment"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
