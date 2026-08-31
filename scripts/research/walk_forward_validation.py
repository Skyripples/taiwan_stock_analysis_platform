"""Run leakage-safe expanding-window validation for the production baseline design."""

from __future__ import annotations

import csv
import json
import logging
import math
import os
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from config import PROJECT_ROOT
from research.common import (
    accuracy_from_rows,
    binary_classification_metrics,
    clean_metric,
    expanding_window_indices,
    window_accuracy,
)


LOGGER = logging.getLogger("walk_forward_validation")
INPUT_PATH = PROJECT_ROOT / "data" / "history" / "historical_prediction_dataset.csv"
OUTPUT_PATH = PROJECT_ROOT / "data" / "analysis" / "current" / "walk_forward_validation.json"
INITIAL_TRAINING_SIZE = 250
FEATURE_NAMES = (
    "taiex_close",
    "taiex_change_percent",
    "tpex_close",
    "turnover",
    "advancing",
    "declining",
    "foreign_cash_flow",
    "foreign_futures_position",
    "night_futures_change",
    "tsm_adr_change_percent",
    "sox_change_percent",
    "sp500_change_percent",
    "nasdaq_change_percent",
    "vix_change_percent",
    "kospi_change_percent",
)


def run_walk_forward_validation(
    input_path: Path = INPUT_PATH,
    output_path: Path = OUTPUT_PATH,
    initial_training_size: int = INITIAL_TRAINING_SIZE,
) -> dict[str, Any]:
    """Fit on rows before each observation and predict that observation once."""

    rows = _load_rows(input_path)
    if initial_training_size < 2 or initial_training_size >= len(rows):
        raise ValueError("Initial training size must leave at least one prediction row")

    try:
        from sklearn.linear_model import LogisticRegression
        from sklearn.pipeline import Pipeline
        from sklearn.preprocessing import StandardScaler
    except ModuleNotFoundError as exc:
        raise RuntimeError("scikit-learn is required; run: pip install -r requirements.txt") from exc

    predictions: list[dict[str, Any]] = []
    for prediction_index in expanding_window_indices(len(rows), initial_training_size):
        training_rows = rows[:prediction_index]
        current = rows[prediction_index]
        training_targets = [row["target_direction"] for row in training_rows]
        if len(set(training_targets)) != 2:
            raise ValueError(f"Training window ending at {training_rows[-1]['feature_date']} does not contain both classes")

        pipeline = Pipeline(
            [
                ("scaler", StandardScaler()),
                ("classifier", LogisticRegression(max_iter=1000, random_state=42)),
            ]
        )
        # Both scaler and classifier are fit only on the expanding historical window.
        pipeline.fit(
            [[row[feature] for feature in FEATURE_NAMES] for row in training_rows],
            training_targets,
        )
        predicted = int(pipeline.predict([[current[feature] for feature in FEATURE_NAMES]])[0])
        probabilities = pipeline.predict_proba(
            [[current[feature] for feature in FEATURE_NAMES]]
        )[0]
        class_probabilities = {
            int(label): float(value)
            for label, value in zip(pipeline.classes_, probabilities)
        }
        if set(class_probabilities) != {0, 1}:
            raise ValueError("Walk-forward classifier does not contain both classes")

        counts = Counter(training_targets)
        # Match the production baseline tie-break: prefer class 1 when counts tie.
        baseline_prediction = max((0, 1), key=lambda label: (counts[label], label))
        actual = current["target_direction"]
        prediction_row = {
            "feature_date": current["feature_date"],
            "target_date": current["target_date"],
            "train_start_feature_date": training_rows[0]["feature_date"],
            "train_end_feature_date": training_rows[-1]["feature_date"],
            "train_end_target_date": training_rows[-1]["target_date"],
            "training_sample_count": len(training_rows),
            "actual": actual,
            "prediction": predicted,
            "up_probability": _clean(class_probabilities[1]),
            "hit": predicted == actual,
            "baseline_prediction": baseline_prediction,
            "baseline_hit": baseline_prediction == actual,
        }
        _validate_prediction_row(prediction_row)
        predictions.append(prediction_row)

    metrics = _metrics(predictions)
    yearly: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in predictions:
        yearly[item["target_date"][:4]].append(item)
    yearly_accuracy = {
        year: {
            "sample_count": len(items),
            "accuracy": _accuracy(items),
        }
        for year, items in sorted(yearly.items())
    }
    payload: dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "version": "1.0",
        "dataset": "historical_prediction_dataset",
        "algorithm": "StandardScaler + LogisticRegression(max_iter=1000, random_state=42)",
        "features": list(FEATURE_NAMES),
        "validation_method": "expanding_window_one_step_ahead",
        "initial_training_size": initial_training_size,
        "total_dataset_samples": len(rows),
        "total_predictions": len(predictions),
        **metrics,
        "recent_accuracy": {
            "last_50": _window_accuracy(predictions, 50),
            "last_100": _window_accuracy(predictions, 100),
        },
        "yearly_accuracy": yearly_accuracy,
        "predictions": predictions,
    }
    _validate_report(payload)
    _write_json_atomic(output_path, payload)
    return payload


def _load_rows(path: Path) -> list[dict[str, Any]]:
    try:
        source = path.open("r", encoding="utf-8-sig", newline="")
    except FileNotFoundError as exc:
        raise ValueError(f"Historical prediction dataset not found: {path}") from exc

    required = {"feature_date", "target_date", "target_direction", *FEATURE_NAMES}
    rows: list[dict[str, Any]] = []
    with source:
        reader = csv.DictReader(source)
        missing = sorted(required.difference(reader.fieldnames or ()))
        if missing:
            raise ValueError("Historical dataset is missing columns: " + ", ".join(missing))
        previous_feature_date = ""
        seen: set[str] = set()
        for row_number, source_row in enumerate(reader, start=2):
            feature_date = str(source_row["feature_date"]).strip()
            target_date = str(source_row["target_date"]).strip()
            if not feature_date or not target_date or target_date <= feature_date:
                raise ValueError(f"Invalid feature/target date at CSV row {row_number}")
            if feature_date in seen or (previous_feature_date and feature_date <= previous_feature_date):
                raise ValueError("Historical dataset dates must be unique and strictly chronological")
            try:
                target = int(str(source_row["target_direction"]).strip())
                values = {
                    feature: float(str(source_row[feature]).strip())
                    for feature in FEATURE_NAMES
                }
            except (TypeError, ValueError) as exc:
                raise ValueError(f"Invalid numeric value at CSV row {row_number}") from exc
            if target not in (0, 1) or not all(math.isfinite(value) for value in values.values()):
                raise ValueError(f"Invalid target or non-finite feature at CSV row {row_number}")
            rows.append(
                {
                    "feature_date": feature_date,
                    "target_date": target_date,
                    "target_direction": target,
                    **values,
                }
            )
            seen.add(feature_date)
            previous_feature_date = feature_date
    return rows


def _metrics(rows: list[dict[str, Any]]) -> dict[str, float]:
    actual = [row["actual"] for row in rows]
    predicted = [row["prediction"] for row in rows]
    probabilities = [row["up_probability"] for row in rows]
    metrics = binary_classification_metrics(actual, predicted, probabilities)
    metrics["baseline_accuracy"] = accuracy_from_rows(rows, "baseline_hit")
    return metrics


def _accuracy(rows: list[dict[str, Any]]) -> float:
    return accuracy_from_rows(rows)


def _window_accuracy(rows: list[dict[str, Any]], window: int) -> dict[str, Any]:
    return window_accuracy(rows, window)


def _clean(value: float) -> float:
    return clean_metric(value)


def _validate_prediction_row(row: dict[str, Any]) -> None:
    if not row["train_end_feature_date"] < row["target_date"]:
        raise ValueError("Training feature endpoint must precede prediction target date")
    if not row["train_end_target_date"] < row["target_date"]:
        raise ValueError("Training target endpoint must precede prediction target date")
    if not 0 <= row["up_probability"] <= 1:
        raise ValueError("Prediction probability must be in [0, 1]")
    if row["hit"] != (row["prediction"] == row["actual"]):
        raise ValueError("Incorrect hit calculation")
    if row["baseline_hit"] != (row["baseline_prediction"] == row["actual"]):
        raise ValueError("Incorrect baseline hit calculation")


def _validate_report(payload: dict[str, Any]) -> None:
    predictions = payload["predictions"]
    if payload["total_predictions"] != len(predictions):
        raise ValueError("Prediction count is inconsistent")
    if payload["initial_training_size"] + len(predictions) != payload["total_dataset_samples"]:
        raise ValueError("Initial training and prediction counts are inconsistent")
    for metric in ("accuracy", "precision", "recall", "f1", "roc_auc", "baseline_accuracy"):
        if not 0 <= payload[metric] <= 1:
            raise ValueError(f"Metric outside [0, 1]: {metric}")
    if sum(item["sample_count"] for item in payload["yearly_accuracy"].values()) != len(predictions):
        raise ValueError("Yearly sample counts are inconsistent")


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as output:
            json.dump(payload, output, ensure_ascii=False, indent=2, allow_nan=False)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(message)s")
    LOGGER.info("Walk-forward validation started")
    try:
        report = run_walk_forward_validation()
    except (OSError, RuntimeError, ValueError) as exc:
        LOGGER.error("Walk-forward validation failed: %s", exc)
        return 1
    LOGGER.info("Walk-forward report written: %s", OUTPUT_PATH)
    LOGGER.info(
        "Walk-forward validation finished | predictions=%d | accuracy=%.4f | roc_auc=%.4f",
        report["total_predictions"], report["accuracy"], report["roc_auc"],
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
