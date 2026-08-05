"""Train the V3.0 Logistic Regression baseline model."""

from __future__ import annotations

import csv
import json
import logging
import os
import pickle
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from config import PROJECT_ROOT


LOGGER = logging.getLogger("baseline_model")
DATASET_PATH = PROJECT_ROOT / "data" / "history" / "prediction_dataset.csv"
MODEL_DIR = PROJECT_ROOT / "models"
MODEL_PATH = MODEL_DIR / "baseline_model.pkl"
MODEL_INFO_PATH = MODEL_DIR / "model_info.json"
MINIMUM_SAMPLE_COUNT = 200
MODEL_VERSION = "1.0"

# Date and target columns are deliberately excluded to prevent leakage.
FEATURE_NAMES = (
    "taiex_close",
    "taiex_change_percent",
    "tpex_close",
    "turnover",
    "advancing",
    "declining",
    "unchanged",
    "foreign_cash_flow",
    "foreign_futures_position",
    "night_futures_change",
    "tsm_adr_change_percent",
    "sox_change_percent",
    "sp500_change_percent",
    "nasdaq_change_percent",
)
TARGET_NAME = "target_direction"


def load_dataset(path: Path = DATASET_PATH) -> tuple[list[list[float]], list[int]]:
    """Load and strictly validate numeric training features and binary targets."""

    try:
        source = path.open("r", encoding="utf-8", newline="")
    except FileNotFoundError as exc:
        raise ValueError(f"Prediction dataset not found: {path}") from exc

    with source:
        reader = csv.DictReader(source)
        headers = set(reader.fieldnames or ())
        required = {*FEATURE_NAMES, TARGET_NAME, "feature_date"}
        missing = sorted(required.difference(headers))
        if missing:
            raise ValueError(f"Prediction dataset is missing columns: {', '.join(missing)}")
        rows = list(reader)

    features: list[list[float]] = []
    targets: list[int] = []
    seen_dates: set[str] = set()
    for row_number, row in enumerate(rows, start=2):
        feature_date = str(row.get("feature_date", "")).strip()
        if not feature_date:
            raise ValueError(f"Missing feature_date at CSV row {row_number}")
        if feature_date in seen_dates:
            raise ValueError(f"Duplicate feature_date: {feature_date}")
        seen_dates.add(feature_date)
        try:
            feature_row = [float(str(row[name]).strip()) for name in FEATURE_NAMES]
            target = int(str(row[TARGET_NAME]).strip())
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Invalid numeric value at CSV row {row_number}") from exc
        if target not in (0, 1):
            raise ValueError(f"target_direction must be 0 or 1 at CSV row {row_number}")
        features.append(feature_row)
        targets.append(target)
    return features, targets


def train_model(dataset_path: Path = DATASET_PATH) -> bool:
    """Train and atomically export the model and its evaluation metadata."""

    features, targets = load_dataset(dataset_path)
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
        from sklearn.metrics import accuracy_score, confusion_matrix, f1_score, precision_score, recall_score
        from sklearn.pipeline import Pipeline
        from sklearn.preprocessing import StandardScaler
    except ModuleNotFoundError as exc:
        raise RuntimeError("scikit-learn is required; run: pip install -r requirements.txt") from exc

    split_index = int(sample_count * 0.8)
    if split_index <= 0 or split_index >= sample_count:
        raise ValueError("Unable to create chronological train/test split")
    train_x, test_x = features[:split_index], features[split_index:]
    train_y, test_y = targets[:split_index], targets[split_index:]
    if len(set(train_y)) < 2:
        raise ValueError("Chronological training split must contain both direction classes")

    pipeline = Pipeline(
        [("scaler", StandardScaler()), ("classifier", LogisticRegression(max_iter=1000, random_state=42))]
    )
    pipeline.fit(train_x, train_y)
    predictions = pipeline.predict(test_x)
    trained_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    model_info: dict[str, Any] = {
        "model_version": MODEL_VERSION,
        "algorithm": "Logistic Regression",
        "accuracy": float(accuracy_score(test_y, predictions)),
        "precision": float(precision_score(test_y, predictions, zero_division=0)),
        "recall": float(recall_score(test_y, predictions, zero_division=0)),
        "f1": float(f1_score(test_y, predictions, zero_division=0)),
        "confusion_matrix": confusion_matrix(test_y, predictions, labels=[0, 1]).tolist(),
        "feature_names": list(FEATURE_NAMES),
        "trained_at": trained_at,
        "sample_count": sample_count,
        "training_sample_count": len(train_x),
        "test_sample_count": len(test_x),
    }
    artifact = {
        "model_version": MODEL_VERSION,
        "feature_names": list(FEATURE_NAMES),
        "trained_at": trained_at,
        "model": pipeline,
    }
    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    _write_pickle_atomic(MODEL_PATH, artifact)
    _write_json_atomic(MODEL_INFO_PATH, model_info)
    LOGGER.info("Baseline model written: %s", MODEL_PATH)
    LOGGER.info("Model metadata written: %s", MODEL_INFO_PATH)
    return True


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
