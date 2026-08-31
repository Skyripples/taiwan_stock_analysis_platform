"""Run fixed-split feature ablation experiments without modifying the production model."""

from __future__ import annotations

import csv
import json
import logging
import math
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from config import PROJECT_ROOT


LOGGER = logging.getLogger("feature_ablation")
INPUT_PATH = PROJECT_ROOT / "data" / "history" / "engineered_prediction_dataset.csv"
OUTPUT_PATH = PROJECT_ROOT / "data" / "analysis" / "archive" / "feature_ablation.json"
TARGET = "target_direction"

ORIGINAL_FEATURES = (
    "taiex_close", "taiex_change_percent", "tpex_close", "turnover",
    "advancing", "declining", "unchanged", "foreign_cash_flow",
    "foreign_futures_position", "night_futures_change",
    "tsm_adr_change_percent", "sox_change_percent",
    "sp500_change_percent", "nasdaq_change_percent",
)
ENGINEERED_FEATURES = (
    "turnover_ratio_20d", "market_breadth", "advance_decline_ratio",
    "foreign_cash_flow_5d_sum", "foreign_cash_flow_20d_zscore",
    "foreign_futures_change_1d", "foreign_futures_change_5d",
    "taiex_return_3d", "taiex_return_5d", "taiex_return_20d",
    "taiex_ma5_distance", "taiex_ma20_distance", "taiex_volatility_20d",
)
TOP_FIVE = (
    "night_futures_change", "sox_change_percent", "nasdaq_change_percent",
    "tsm_adr_change_percent", "sp500_change_percent",
)
CANDIDATE_ENGINEERED = (
    "foreign_futures_change_1d", "foreign_futures_change_5d",
    "turnover_ratio_20d", "taiex_return_5d", "taiex_ma5_distance",
)
ALL_FEATURES = (*ORIGINAL_FEATURES, *ENGINEERED_FEATURES)
METRICS = ("accuracy", "precision", "recall", "f1", "roc_auc")


def analyze_feature_ablation(
    input_path: Path = INPUT_PATH,
    output_path: Path = OUTPUT_PATH,
) -> dict[str, Any]:
    """Evaluate predefined feature sets and leave-one-feature-out variants."""

    rows = _load_rows(input_path)
    split_index = int(len(rows) * 0.8)
    if split_index <= 0 or split_index >= len(rows):
        raise ValueError("Unable to create chronological 80/20 split")
    train_rows, test_rows = rows[:split_index], rows[split_index:]
    if len({row[TARGET] for row in train_rows}) < 2:
        raise ValueError("Training split must contain both target classes")
    if len({row[TARGET] for row in test_rows}) < 2:
        raise ValueError("Test split must contain both target classes for ROC AUC")

    feature_sets = {
        "A_original_14": ORIGINAL_FEATURES,
        "B_original_plus_all_engineered": ALL_FEATURES,
        "C_original_top_5": TOP_FIVE,
        "D_original_plus_candidates": (*ORIGINAL_FEATURES, *CANDIDATE_ENGINEERED),
    }
    models = {
        name: _evaluate(train_rows, test_rows, features)
        for name, features in feature_sets.items()
    }
    model_a = models["A_original_14"]
    model_b = models["B_original_plus_all_engineered"]
    b_vs_a = {metric: _clean(model_b[metric] - model_a[metric]) for metric in METRICS}

    leave_one_out = []
    for removed in ALL_FEATURES:
        selected = tuple(feature for feature in ALL_FEATURES if feature != removed)
        result = _evaluate(train_rows, test_rows, selected)
        leave_one_out.append({
            "removed_feature": removed,
            **result,
            "delta_vs_B": {metric: _clean(result[metric] - model_b[metric]) for metric in METRICS},
        })

    # Ranking is descriptive only. It is not used to select or retrain a model.
    ranked = sorted(
        leave_one_out,
        key=lambda item: (
            -item["delta_vs_B"]["accuracy"],
            -item["delta_vs_B"]["roc_auc"],
            item["removed_feature"],
        ),
    )
    payload: dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "version": "1.0",
        "dataset": "engineered_prediction_dataset",
        "algorithm": "StandardScaler + LogisticRegression(max_iter=1000, random_state=42)",
        "split_method": "chronological_80_20_no_shuffle",
        "selection_policy": "fixed experiment only; test results are not used to modify the production model",
        "sample_count": len(rows),
        "training_sample_count": len(train_rows),
        "test_sample_count": len(test_rows),
        "train_date_range": {
            "feature_start": train_rows[0]["feature_date"],
            "feature_end": train_rows[-1]["feature_date"],
            "target_start": train_rows[0]["target_date"],
            "target_end": train_rows[-1]["target_date"],
        },
        "test_date_range": {
            "feature_start": test_rows[0]["feature_date"],
            "feature_end": test_rows[-1]["feature_date"],
            "target_start": test_rows[0]["target_date"],
            "target_end": test_rows[-1]["target_date"],
        },
        "models": models,
        "B_vs_A_delta": b_vs_a,
        "leave_one_feature_out": leave_one_out,
        "leave_one_out_ranked_by_accuracy_delta": [
            {"rank": index, **item} for index, item in enumerate(ranked, start=1)
        ],
    }
    _validate_report(payload)
    _write_json_atomic(output_path, payload)
    return payload


def _load_rows(path: Path) -> list[dict[str, Any]]:
    try:
        handle = path.open("r", encoding="utf-8-sig", newline="")
    except FileNotFoundError as exc:
        raise ValueError(f"Engineered prediction dataset not found: {path}") from exc
    required = {"feature_date", "target_date", TARGET, *ALL_FEATURES}
    rows: list[dict[str, Any]] = []
    with handle:
        reader = csv.DictReader(handle)
        missing = sorted(required.difference(reader.fieldnames or ()))
        if missing:
            raise ValueError("Dataset is missing columns: " + ", ".join(missing))
        previous_date = ""
        seen: set[str] = set()
        for row_number, source in enumerate(reader, start=2):
            feature_date = str(source["feature_date"]).strip()
            target_date = str(source["target_date"]).strip()
            if not feature_date or not target_date or target_date <= feature_date:
                raise ValueError(f"Invalid feature/target date at row {row_number}")
            if feature_date in seen or (previous_date and feature_date <= previous_date):
                raise ValueError("Dataset dates must be unique and strictly chronological")
            try:
                target = int(str(source[TARGET]).strip())
                values = {feature: float(str(source[feature]).strip()) for feature in ALL_FEATURES}
            except (TypeError, ValueError) as exc:
                raise ValueError(f"Invalid numeric value at row {row_number}") from exc
            if target not in (0, 1) or not all(math.isfinite(value) for value in values.values()):
                raise ValueError(f"Invalid target or non-finite feature at row {row_number}")
            rows.append({"feature_date": feature_date, "target_date": target_date, TARGET: target, **values})
            seen.add(feature_date)
            previous_date = feature_date
    if len(rows) < 2:
        raise ValueError("At least two samples are required")
    return rows


def _evaluate(
    train_rows: list[dict[str, Any]],
    test_rows: list[dict[str, Any]],
    features: Iterable[str],
) -> dict[str, Any]:
    try:
        from sklearn.linear_model import LogisticRegression
        from sklearn.metrics import accuracy_score, f1_score, precision_score, recall_score, roc_auc_score
        from sklearn.pipeline import Pipeline
        from sklearn.preprocessing import StandardScaler
    except ModuleNotFoundError as exc:
        raise RuntimeError("scikit-learn is required; run: pip install -r requirements.txt") from exc

    selected = tuple(features)
    train_x = [[row[feature] for feature in selected] for row in train_rows]
    test_x = [[row[feature] for feature in selected] for row in test_rows]
    train_y = [row[TARGET] for row in train_rows]
    test_y = [row[TARGET] for row in test_rows]
    pipeline = Pipeline([
        ("scaler", StandardScaler()),
        ("classifier", LogisticRegression(max_iter=1000, random_state=42)),
    ])
    # Pipeline.fit ensures the scaler sees only the chronological Train split.
    pipeline.fit(train_x, train_y)
    predictions = pipeline.predict(test_x)
    probabilities = pipeline.predict_proba(test_x)
    class_indexes = {int(label): index for index, label in enumerate(pipeline.classes_)}
    if set(class_indexes) != {0, 1}:
        raise ValueError("Trained classifier must contain both target classes")
    up_probabilities = probabilities[:, class_indexes[1]]
    return {
        "feature_count": len(selected),
        "features": list(selected),
        "accuracy": _clean(accuracy_score(test_y, predictions)),
        "precision": _clean(precision_score(test_y, predictions, zero_division=0)),
        "recall": _clean(recall_score(test_y, predictions, zero_division=0)),
        "f1": _clean(f1_score(test_y, predictions, zero_division=0)),
        "roc_auc": _clean(roc_auc_score(test_y, up_probabilities)),
    }


def _clean(value: float) -> float:
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("Ablation metric is not finite")
    return round(result, 12)


def _validate_report(payload: dict[str, Any]) -> None:
    for result in payload["models"].values():
        for metric in METRICS:
            if not 0 <= result[metric] <= 1:
                raise ValueError(f"Metric outside [0, 1]: {metric}")
    leave_one_out = payload["leave_one_feature_out"]
    if len(leave_one_out) != len(ALL_FEATURES):
        raise ValueError("Leave-one-out result count is incomplete")
    removed = [item["removed_feature"] for item in leave_one_out]
    if len(removed) != len(set(removed)) or set(removed) != set(ALL_FEATURES):
        raise ValueError("Leave-one-out features are missing or duplicated")
    expected_train = payload["training_sample_count"]
    expected_test = payload["test_sample_count"]
    if expected_train + expected_test != payload["sample_count"]:
        raise ValueError("Train/test sample counts are inconsistent")


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
    LOGGER.info("Feature ablation started")
    try:
        report = analyze_feature_ablation()
    except (OSError, RuntimeError, ValueError) as exc:
        LOGGER.error("Feature ablation failed: %s", exc)
        return 1
    LOGGER.info("Feature ablation report written: %s", OUTPUT_PATH)
    LOGGER.info(
        "Feature ablation finished | samples=%d | train=%d | test=%d | experiments=%d",
        report["sample_count"], report["training_sample_count"],
        report["test_sample_count"], 4 + len(report["leave_one_feature_out"]),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
