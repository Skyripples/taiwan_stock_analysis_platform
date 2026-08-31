"""Train the chronological V3.6 Logistic Regression historical baseline."""

from __future__ import annotations

import csv
import json
import logging
import math
import os
import pickle
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from config import PROJECT_ROOT


LOGGER = logging.getLogger("baseline_model")
DATASET_PATH = PROJECT_ROOT / "data" / "history" / "historical_prediction_dataset.csv"
MODEL_DIR = PROJECT_ROOT / "models"
MODEL_PATH = MODEL_DIR / "baseline_model.pkl"
MODEL_INFO_PATH = MODEL_DIR / "model_info.json"
CALIBRATION_PATH = MODEL_DIR / "platt_calibrator.pkl"
WALK_FORWARD_PATH = PROJECT_ROOT / "data" / "analysis" / "current" / "walk_forward_validation.json"
MINIMUM_SAMPLE_COUNT = 200
MODEL_VERSION = "3.4"

# Date and target columns are deliberately excluded to prevent leakage.
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
TARGET_NAME = "target_direction"


def load_dataset(
    path: Path = DATASET_PATH,
) -> tuple[list[list[float]], list[int]]:
    """Backward-compatible loader returning only features and targets."""

    features, targets, _, _ = _load_dataset_with_dates(path)
    return features, targets


def _load_dataset_with_dates(
    path: Path = DATASET_PATH,
) -> tuple[list[list[float]], list[int], list[str], list[str]]:
    """Load and strictly validate numeric training features and binary targets."""

    try:
        source = path.open("r", encoding="utf-8-sig", newline="")
    except FileNotFoundError as exc:
        raise ValueError(f"Prediction dataset not found: {path}") from exc

    with source:
        reader = csv.DictReader(source)
        headers = set(reader.fieldnames or ())
        required = {*FEATURE_NAMES, TARGET_NAME, "feature_date", "target_date"}
        missing = sorted(required.difference(headers))
        if missing:
            raise ValueError(f"Prediction dataset is missing columns: {', '.join(missing)}")
        rows = list(reader)

    features: list[list[float]] = []
    targets: list[int] = []
    feature_dates: list[str] = []
    target_dates: list[str] = []
    seen_dates: set[str] = set()
    previous_feature_date = None
    for row_number, row in enumerate(rows, start=2):
        feature_date = str(row.get("feature_date", "")).strip()
        target_date = str(row.get("target_date", "")).strip()
        try:
            parsed_feature_date = datetime.strptime(feature_date, "%Y-%m-%d").date()
            parsed_target_date = datetime.strptime(target_date, "%Y-%m-%d").date()
        except ValueError as exc:
            raise ValueError(f"Invalid feature/target date at CSV row {row_number}") from exc
        if feature_date in seen_dates:
            raise ValueError(f"Duplicate feature_date: {feature_date}")
        if previous_feature_date is not None and parsed_feature_date <= previous_feature_date:
            raise ValueError("Historical dataset must be strictly chronological")
        if parsed_target_date <= parsed_feature_date:
            raise ValueError(f"target_date must follow feature_date at CSV row {row_number}")
        seen_dates.add(feature_date)
        previous_feature_date = parsed_feature_date
        try:
            feature_row = [float(str(row[name]).strip()) for name in FEATURE_NAMES]
            target = int(str(row[TARGET_NAME]).strip())
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Invalid numeric value at CSV row {row_number}") from exc
        if target not in (0, 1):
            raise ValueError(f"target_direction must be 0 or 1 at CSV row {row_number}")
        features.append(feature_row)
        targets.append(target)
        feature_dates.append(feature_date)
        target_dates.append(target_date)
    return features, targets, feature_dates, target_dates


def train_model(dataset_path: Path = DATASET_PATH) -> bool:
    """Train and atomically export the model and its evaluation metadata."""

    features, targets, feature_dates, target_dates = _load_dataset_with_dates(dataset_path)
    sample_count = len(features)
    if sample_count < MINIMUM_SAMPLE_COUNT:
        LOGGER.warning(
            "Insufficient training data: %d / %d samples; model was not trained",
            sample_count,
            MINIMUM_SAMPLE_COUNT,
        )
        return False
    if len(set(targets)) < 2:
        raise ValueError("Training target must contain both direction classes")

    try:
        from sklearn.linear_model import LogisticRegression
        from sklearn.metrics import (
            accuracy_score,
            confusion_matrix,
            f1_score,
            precision_score,
            recall_score,
            roc_auc_score,
        )
        from sklearn.pipeline import Pipeline
        from sklearn.preprocessing import StandardScaler
    except ModuleNotFoundError as exc:
        raise RuntimeError("scikit-learn is required; run: pip install -r requirements.txt") from exc

    split_index = int(sample_count * 0.8)
    if split_index <= 0 or split_index >= sample_count:
        raise ValueError("Unable to create chronological train/test split")
    train_x, test_x = features[:split_index], features[split_index:]
    train_y, test_y = targets[:split_index], targets[split_index:]
    train_feature_dates, test_feature_dates = feature_dates[:split_index], feature_dates[split_index:]
    train_target_dates, test_target_dates = target_dates[:split_index], target_dates[split_index:]
    if len(set(train_y)) < 2:
        raise ValueError("Chronological training split must contain both direction classes")

    pipeline = Pipeline(
        [("scaler", StandardScaler()), ("classifier", LogisticRegression(max_iter=1000, random_state=42))]
    )
    pipeline.fit(train_x, train_y)
    predictions = pipeline.predict(test_x)
    probabilities = pipeline.predict_proba(test_x)
    class_indexes = {int(label): index for index, label in enumerate(pipeline.classes_)}
    if 0 not in class_indexes or 1 not in class_indexes:
        raise ValueError("Trained classifier must contain both direction classes")
    if len(set(test_y)) < 2:
        raise ValueError("Chronological test split must contain both classes for ROC AUC")
    up_probabilities = probabilities[:, class_indexes[1]]

    train_class_counts = {label: train_y.count(label) for label in (0, 1)}
    majority_class = max(train_class_counts, key=lambda label: (train_class_counts[label], label))
    baseline_predictions = [majority_class] * len(test_y)
    baseline_accuracy = float(accuracy_score(test_y, baseline_predictions))
    model_accuracy = float(accuracy_score(test_y, predictions))
    classifier = pipeline.named_steps["classifier"]
    coefficients = {
        feature_name: float(coefficient)
        for feature_name, coefficient in zip(FEATURE_NAMES, classifier.coef_[0])
    }
    trained_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    model_info: dict[str, Any] = {
        "model_version": MODEL_VERSION,
        "algorithm": "StandardScaler + Logistic Regression",
        "split_method": "chronological_80_20_no_shuffle",
        "accuracy": model_accuracy,
        "precision": float(precision_score(test_y, predictions, zero_division=0)),
        "recall": float(recall_score(test_y, predictions, zero_division=0)),
        "f1": float(f1_score(test_y, predictions, zero_division=0)),
        "roc_auc": float(roc_auc_score(test_y, up_probabilities)),
        "confusion_matrix": confusion_matrix(test_y, predictions, labels=[0, 1]).tolist(),
        "baseline_accuracy": baseline_accuracy,
        "baseline_strategy": "always_predict_training_majority_class",
        "baseline_majority_class": int(majority_class),
        "accuracy_vs_baseline": model_accuracy - baseline_accuracy,
        "feature_names": list(FEATURE_NAMES),
        "coefficients": coefficients,
        "trained_at": trained_at,
        "sample_count": sample_count,
        "training_sample_count": len(train_x),
        "test_sample_count": len(test_x),
        "train_date_range": {
            "feature_start": train_feature_dates[0],
            "feature_end": train_feature_dates[-1],
            "target_start": train_target_dates[0],
            "target_end": train_target_dates[-1],
        },
        "test_date_range": {
            "feature_start": test_feature_dates[0],
            "feature_end": test_feature_dates[-1],
            "target_start": test_target_dates[0],
            "target_end": test_target_dates[-1],
        },
    }
    artifact = {
        "model_version": MODEL_VERSION,
        "feature_names": list(FEATURE_NAMES),
        "trained_at": trained_at,
        "model": pipeline,
    }
    calibration_artifact, calibration_info = _fit_platt_calibrator(WALK_FORWARD_PATH)
    model_info.update(calibration_info)
    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    _write_pickle_atomic(CALIBRATION_PATH, calibration_artifact)
    _write_pickle_atomic(MODEL_PATH, artifact)
    _write_json_atomic(MODEL_INFO_PATH, model_info)
    LOGGER.info("Baseline model written: %s", MODEL_PATH)
    LOGGER.info("Platt calibration artifact written: %s", CALIBRATION_PATH)
    LOGGER.info("Model metadata written: %s", MODEL_INFO_PATH)
    return True


def _fit_platt_calibrator(path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    """Fit Platt on early OOS predictions and evaluate only on the held-out tail."""

    try:
        with path.open("r", encoding="utf-8") as source:
            report = json.load(source)
    except FileNotFoundError as exc:
        raise ValueError(f"Walk-forward calibration source not found: {path}") from exc
    rows = report.get("predictions") if isinstance(report, dict) else None
    if not isinstance(rows, list) or report.get("total_predictions") != len(rows):
        raise ValueError("Walk-forward calibration predictions are missing or inconsistent")
    split_index = int(len(rows) * 0.7)
    fit_rows, test_rows = rows[:split_index], rows[split_index:]
    if not fit_rows or not test_rows:
        raise ValueError("Unable to create chronological calibration split")
    if str(fit_rows[-1].get("target_date", "")) >= str(test_rows[0].get("target_date", "")):
        raise ValueError("Calibration fit period overlaps final calibration test")
    try:
        from sklearn.linear_model import LogisticRegression
        from sklearn.metrics import brier_score_loss, log_loss, roc_auc_score
    except ModuleNotFoundError as exc:
        raise RuntimeError("scikit-learn is required; run: pip install -r requirements.txt") from exc

    fit_probability, fit_target = _calibration_values(fit_rows)
    test_probability, test_target = _calibration_values(test_rows)
    calibrator = LogisticRegression(max_iter=1000, random_state=42)
    calibrator.fit([[_probability_logit(value)] for value in fit_probability], fit_target)
    class_indexes = {int(label): index for index, label in enumerate(calibrator.classes_)}
    if set(class_indexes) != {0, 1}:
        raise ValueError("Platt calibrator must contain both classes")
    calibrated = [
        float(values[class_indexes[1]])
        for values in calibrator.predict_proba(
            [[_probability_logit(value)] for value in test_probability]
        )
    ]
    raw_metrics = _calibration_metrics(test_probability, test_target, brier_score_loss, log_loss, roc_auc_score)
    calibrated_metrics = _calibration_metrics(calibrated, test_target, brier_score_loss, log_loss, roc_auc_score)
    artifact = {
        "calibration_method": "platt",
        "calibration_version": "1.0",
        "model_version": MODEL_VERSION,
        "input": "raw_up_probability_logit",
        "fit_sample_count": len(fit_rows),
        "fit_target_start": fit_rows[0]["target_date"],
        "fit_target_end": fit_rows[-1]["target_date"],
        "held_out_test_sample_count": len(test_rows),
        "held_out_test_target_start": test_rows[0]["target_date"],
        "held_out_test_target_end": test_rows[-1]["target_date"],
        "calibrator": calibrator,
    }
    info = {
        "calibration_method": "platt",
        "calibration_fit_samples": len(fit_rows),
        "calibration_fit_date_range": {
            "target_start": fit_rows[0]["target_date"],
            "target_end": fit_rows[-1]["target_date"],
        },
        "calibration_test_samples": len(test_rows),
        "calibration_test_date_range": {
            "target_start": test_rows[0]["target_date"],
            "target_end": test_rows[-1]["target_date"],
        },
        "calibration_metrics": {
            "uncalibrated": raw_metrics,
            "platt": calibrated_metrics,
        },
    }
    return artifact, info


def _calibration_values(rows: list[dict[str, Any]]) -> tuple[list[float], list[int]]:
    probabilities: list[float] = []
    targets: list[int] = []
    for index, row in enumerate(rows, start=1):
        try:
            probability = float(row["up_probability"])
            target = int(row["actual"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"Invalid calibration row #{index}") from exc
        if not math.isfinite(probability) or not 0 <= probability <= 1 or target not in (0, 1):
            raise ValueError(f"Invalid calibration value at row #{index}")
        probabilities.append(probability)
        targets.append(target)
    if len(set(targets)) != 2:
        raise ValueError("Calibration segment must contain both classes")
    return probabilities, targets


def _probability_logit(probability: float) -> float:
    clipped = min(1 - 1e-15, max(1e-15, probability))
    return math.log(clipped / (1 - clipped))


def _calibration_metrics(
    probabilities: list[float],
    targets: list[int],
    brier_score_loss: Any,
    log_loss: Any,
    roc_auc_score: Any,
) -> dict[str, float]:
    buckets: list[list[int]] = [[] for _ in range(10)]
    for index, value in enumerate(probabilities):
        buckets[min(int(value * 10), 9)].append(index)
    ece = math.fsum(
        len(selected) / len(targets)
        * abs(
            math.fsum(probabilities[index] for index in selected) / len(selected)
            - math.fsum(targets[index] for index in selected) / len(selected)
        )
        for selected in buckets
        if selected
    )
    return {
        "brier_score": float(brier_score_loss(targets, probabilities)),
        "log_loss": float(log_loss(targets, probabilities, labels=[0, 1])),
        "expected_calibration_error": float(ece),
        "roc_auc": float(roc_auc_score(targets, probabilities)),
    }


def _write_pickle_atomic(path: Path, payload: Any) -> None:
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    try:
        with temporary.open("wb") as output:
            pickle.dump(payload, output)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as output:
            json.dump(payload, output, ensure_ascii=False, indent=2)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(message)s")
    LOGGER.info("Baseline model training started")
    try:
        trained = train_model()
    except (OSError, RuntimeError, ValueError) as exc:
        LOGGER.error("Baseline model training failed: %s", exc)
        return 1
    if not trained:
        LOGGER.info("Baseline model training finished without creating a model")
        return 0
    LOGGER.info("Baseline model training finished")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
