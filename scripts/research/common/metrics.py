"""Common binary-classification metrics used by research validations."""

from __future__ import annotations

import math
from typing import Any, Iterable, Mapping


def clean_metric(value: float) -> float:
    """Return a finite, consistently rounded metric."""

    result = float(value)
    if not math.isfinite(result):
        raise ValueError("Validation metric is not finite")
    return round(result, 12)


def binary_classification_metrics(
    actual: Iterable[int],
    predicted: Iterable[int],
    up_probability: Iterable[float],
) -> dict[str, float]:
    """Calculate the shared classification metric contract."""

    try:
        from sklearn.metrics import accuracy_score, f1_score, precision_score, recall_score, roc_auc_score
    except ModuleNotFoundError as exc:
        raise RuntimeError("scikit-learn is required; run: pip install -r requirements.txt") from exc

    actual_values = list(actual)
    predicted_values = list(predicted)
    probability_values = list(up_probability)
    if not actual_values or len(actual_values) != len(predicted_values) or len(actual_values) != len(probability_values):
        raise ValueError("Metric inputs must be non-empty and have equal lengths")
    if len(set(actual_values)) != 2:
        raise ValueError("Validation outcomes must contain both classes for ROC AUC")
    return {
        "accuracy": clean_metric(accuracy_score(actual_values, predicted_values)),
        "precision": clean_metric(precision_score(actual_values, predicted_values, zero_division=0)),
        "recall": clean_metric(recall_score(actual_values, predicted_values, zero_division=0)),
        "f1": clean_metric(f1_score(actual_values, predicted_values, zero_division=0)),
        "roc_auc": clean_metric(roc_auc_score(actual_values, probability_values)),
    }


def accuracy_from_rows(rows: Iterable[Mapping[str, Any]], hit_key: str = "hit") -> float:
    """Calculate accuracy from rows containing a boolean hit field."""

    values = list(rows)
    if not values:
        raise ValueError("Accuracy requires at least one row")
    return clean_metric(sum(bool(row[hit_key]) for row in values) / len(values))


def window_accuracy(rows: list[Mapping[str, Any]], window: int, hit_key: str = "hit") -> dict[str, Any]:
    """Return sample count and accuracy for the trailing validation window."""

    if window <= 0:
        raise ValueError("Window must be positive")
    selected = rows[-window:]
    return {"sample_count": len(selected), "accuracy": accuracy_from_rows(selected, hit_key)}
