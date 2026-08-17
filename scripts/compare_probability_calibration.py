"""Chronologically compare uncalibrated, Platt, and isotonic probabilities."""

from __future__ import annotations

import json
import logging
import math
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from config import PROJECT_ROOT


LOGGER = logging.getLogger("calibration_comparison")
INPUT_PATH = PROJECT_ROOT / "data" / "analysis" / "walk_forward_validation.json"
OUTPUT_PATH = PROJECT_ROOT / "data" / "analysis" / "calibration_comparison.json"
CALIBRATION_FRACTION = 0.7
EPSILON = 1e-15


def compare_probability_calibration(
    input_path: Path = INPUT_PATH,
    output_path: Path = OUTPUT_PATH,
) -> dict[str, Any]:
    """Fit calibrators on the earlier OOS segment and evaluate on the later segment."""

    rows = _load_rows(input_path)
    split_index = int(len(rows) * CALIBRATION_FRACTION)
    if split_index <= 0 or split_index >= len(rows):
        raise ValueError("Unable to create chronological calibration split")
    fit_rows, test_rows = rows[:split_index], rows[split_index:]
    fit_probabilities = [row["up_probability"] for row in fit_rows]
    fit_targets = [row["actual"] for row in fit_rows]
    test_probabilities = [row["up_probability"] for row in test_rows]
    test_targets = [row["actual"] for row in test_rows]
    if len(set(fit_targets)) != 2 or len(set(test_targets)) != 2:
        raise ValueError("Calibration fit and test segments must both contain two classes")

    try:
        from sklearn.isotonic import IsotonicRegression
        from sklearn.linear_model import LogisticRegression
    except ModuleNotFoundError as exc:
        raise RuntimeError("scikit-learn is required; run: pip install -r requirements.txt") from exc

    # Platt scaling fits a sigmoid to the original model's log-odds.
    fit_logits = [[_logit(value)] for value in fit_probabilities]
    test_logits = [[_logit(value)] for value in test_probabilities]
    platt = LogisticRegression(max_iter=1000, random_state=42)
    platt.fit(fit_logits, fit_targets)
    platt_indexes = {int(label): index for index, label in enumerate(platt.classes_)}
    platt_probabilities = [
        float(values[platt_indexes[1]])
        for values in platt.predict_proba(test_logits)
    ]

    isotonic = IsotonicRegression(y_min=0.0, y_max=1.0, out_of_bounds="clip")
    isotonic.fit(fit_probabilities, fit_targets)
    isotonic_probabilities = [float(value) for value in isotonic.predict(test_probabilities)]

    method_probabilities = {
        "uncalibrated": test_probabilities,
        "platt_sigmoid": platt_probabilities,
        "isotonic": isotonic_probabilities,
    }
    methods = {
        name: _evaluate(probabilities, test_targets)
        for name, probabilities in method_probabilities.items()
    }
    payload: dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "version": "1.0",
        "source": "walk_forward_validation",
        "analysis_only": True,
        "split_method": "chronological_70_30_no_shuffle",
        "total_oos_predictions": len(rows),
        "calibration_fit_sample_count": len(fit_rows),
        "calibration_test_sample_count": len(test_rows),
        "calibration_fit_date_range": {
            "feature_start": fit_rows[0]["feature_date"],
            "feature_end": fit_rows[-1]["feature_date"],
            "target_start": fit_rows[0]["target_date"],
            "target_end": fit_rows[-1]["target_date"],
        },
        "calibration_test_date_range": {
            "feature_start": test_rows[0]["feature_date"],
            "feature_end": test_rows[-1]["feature_date"],
            "target_start": test_rows[0]["target_date"],
            "target_end": test_rows[-1]["target_date"],
        },
        "fit_configuration": {
            "platt_sigmoid": "LogisticRegression(max_iter=1000, random_state=42) on original probability log-odds",
            "isotonic": "IsotonicRegression(y_min=0, y_max=1, out_of_bounds=clip)",
        },
        "methods": methods,
        "best_method": {
            "brier_score": min(methods, key=lambda name: methods[name]["brier_score"]),
            "log_loss": min(methods, key=lambda name: methods[name]["log_loss"]),
            "expected_calibration_error": min(
                methods, key=lambda name: methods[name]["expected_calibration_error"]
            ),
        },
        "test_predictions": [
            {
                "feature_date": row["feature_date"],
                "target_date": row["target_date"],
                "actual": row["actual"],
                **{
                    # Preserve full float precision so all reported metrics can
                    # be reproduced from these test probabilities.
                    f"{method}_up_probability": float(probabilities[index])
                    for method, probabilities in method_probabilities.items()
                },
            }
            for index, row in enumerate(test_rows)
        ],
    }
    _validate_report(payload)
    _write_json_atomic(output_path, payload)
    return payload


def _load_rows(path: Path) -> list[dict[str, Any]]:
    try:
        with path.open("r", encoding="utf-8") as source:
            report = json.load(source)
    except FileNotFoundError as exc:
        raise ValueError(f"Walk-forward report not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid walk-forward JSON: {path}") from exc
    source_rows = report.get("predictions") if isinstance(report, dict) else None
    if not isinstance(source_rows, list) or report.get("total_predictions") != len(source_rows):
        raise ValueError("Walk-forward predictions are missing or inconsistent")
    rows = []
    previous_feature_date = ""
    for index, item in enumerate(source_rows, start=1):
        try:
            probability = float(item["up_probability"])
            actual = int(item["actual"])
            feature_date = str(item["feature_date"])
            target_date = str(item["target_date"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"Invalid walk-forward prediction #{index}") from exc
        if not math.isfinite(probability) or not 0 <= probability <= 1 or actual not in (0, 1):
            raise ValueError(f"Invalid probability or target at prediction #{index}")
        if not feature_date or not target_date or target_date <= feature_date:
            raise ValueError(f"Invalid dates at prediction #{index}")
        if previous_feature_date and feature_date <= previous_feature_date:
            raise ValueError("Walk-forward predictions must be strictly chronological")
        rows.append(
            {
                "feature_date": feature_date,
                "target_date": target_date,
                "actual": actual,
                "up_probability": probability,
            }
        )
        previous_feature_date = feature_date
    if len(rows) < 2:
        raise ValueError("At least two OOS predictions are required")
    return rows


def _evaluate(probabilities: list[float], actual: list[int]) -> dict[str, Any]:
    try:
        from sklearn.metrics import accuracy_score, log_loss as sklearn_log_loss, roc_auc_score
    except ModuleNotFoundError as exc:
        raise RuntimeError("scikit-learn is required; run: pip install -r requirements.txt") from exc
    if len(probabilities) != len(actual) or not probabilities:
        raise ValueError("Evaluation inputs are inconsistent")
    predicted = [1 if value >= 0.5 else 0 for value in probabilities]
    brier = math.fsum((probability - target) ** 2 for probability, target in zip(probabilities, actual)) / len(actual)
    log_loss = float(sklearn_log_loss(actual, probabilities, labels=[0, 1]))
    buckets = _calibration_buckets(probabilities, actual)
    ece = math.fsum(
        bucket["sample_count"] / len(actual) * bucket["absolute_calibration_error"]
        for bucket in buckets
        if bucket["sample_count"] > 0
    )
    confidence = [max(value, 1 - value) for value in probabilities]
    return {
        "sample_count": len(actual),
        "accuracy": _clean(accuracy_score(actual, predicted)),
        "roc_auc": _clean(roc_auc_score(actual, probabilities)),
        "brier_score": _clean(brier),
        "log_loss": _clean(log_loss),
        "expected_calibration_error": _clean(ece),
        "average_confidence": _clean(math.fsum(confidence) / len(confidence)),
        "confidence_at_least_80_percent": _threshold_accuracy(confidence, predicted, actual, 0.8),
        "confidence_at_least_90_percent": _threshold_accuracy(confidence, predicted, actual, 0.9),
        "calibration_buckets": buckets,
    }


def _calibration_buckets(probabilities: list[float], actual: list[int]) -> list[dict[str, Any]]:
    result = []
    for index in range(10):
        selected = [
            position for position, value in enumerate(probabilities)
            if index / 10 <= value < (index + 1) / 10 or (index == 9 and value == 1)
        ]
        if selected:
            average = math.fsum(probabilities[position] for position in selected) / len(selected)
            rate = math.fsum(actual[position] for position in selected) / len(selected)
            error = abs(average - rate)
        else:
            average = rate = error = None
        result.append(
            {
                "label": f"{index * 10}-{(index + 1) * 10}%",
                "sample_count": len(selected),
                "average_predicted_probability": _clean(average) if average is not None else None,
                "actual_up_rate": _clean(rate) if rate is not None else None,
                "absolute_calibration_error": _clean(error) if error is not None else None,
            }
        )
    return result


def _threshold_accuracy(
    confidence: list[float], predicted: list[int], actual: list[int], threshold: float
) -> dict[str, Any]:
    selected = [index for index, value in enumerate(confidence) if value >= threshold]
    return {
        "threshold": threshold,
        "sample_count": len(selected),
        "average_confidence": (
            _clean(math.fsum(confidence[index] for index in selected) / len(selected))
            if selected else None
        ),
        "accuracy": (
            _clean(math.fsum(predicted[index] == actual[index] for index in selected) / len(selected))
            if selected else None
        ),
    }


def _logit(probability: float) -> float:
    value = _clip(probability)
    return math.log(value / (1 - value))


def _clip(value: float) -> float:
    return min(1 - EPSILON, max(EPSILON, value))


def _clean(value: float) -> float:
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("Calibration comparison contains a non-finite value")
    return round(result, 12)


def _validate_report(payload: dict[str, Any]) -> None:
    if payload["calibration_fit_sample_count"] + payload["calibration_test_sample_count"] != payload["total_oos_predictions"]:
        raise ValueError("Calibration split counts are inconsistent")
    if not payload["calibration_fit_date_range"]["target_end"] < payload["calibration_test_date_range"]["target_start"]:
        raise ValueError("Calibration fit target dates overlap the calibration test period")
    test_count = payload["calibration_test_sample_count"]
    for method, result in payload["methods"].items():
        if result["sample_count"] != test_count:
            raise ValueError(f"Test sample count mismatch for {method}")
        for metric in ("accuracy", "roc_auc", "brier_score", "expected_calibration_error"):
            if not 0 <= result[metric] <= 1:
                raise ValueError(f"Invalid {metric} for {method}")
        if result["log_loss"] < 0:
            raise ValueError(f"Invalid log loss for {method}")
        if sum(bucket["sample_count"] for bucket in result["calibration_buckets"]) != test_count:
            raise ValueError(f"Calibration buckets do not cover all test rows for {method}")
    if len(payload["test_predictions"]) != test_count:
        raise ValueError("Calibration test prediction count is inconsistent")


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
    LOGGER.info("Calibration comparison started")
    try:
        report = compare_probability_calibration()
    except (OSError, RuntimeError, ValueError) as exc:
        LOGGER.error("Calibration comparison failed: %s", exc)
        return 1
    LOGGER.info("Calibration comparison report written: %s", OUTPUT_PATH)
    LOGGER.info(
        "Calibration comparison finished | fit=%d | test=%d | best_brier=%s | best_ece=%s",
        report["calibration_fit_sample_count"],
        report["calibration_test_sample_count"],
        report["best_method"]["brier_score"],
        report["best_method"]["expected_calibration_error"],
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
