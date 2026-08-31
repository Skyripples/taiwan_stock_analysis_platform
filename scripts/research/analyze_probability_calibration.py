"""Analyze calibration of out-of-sample walk-forward probabilities."""

from __future__ import annotations

import json
import logging
import math
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from config import PROJECT_ROOT


LOGGER = logging.getLogger("probability_calibration")
INPUT_PATH = PROJECT_ROOT / "data" / "analysis" / "current" / "walk_forward_validation.json"
OUTPUT_PATH = PROJECT_ROOT / "data" / "analysis" / "archive" / "probability_calibration.json"
PROBABILITY_BUCKET_WIDTH = 0.1
CONFIDENCE_BUCKETS = (
    (0.5, 0.6),
    (0.6, 0.7),
    (0.7, 0.8),
    (0.8, 0.9),
    (0.9, 1.0),
)
EPSILON = 1e-15


def analyze_probability_calibration(
    input_path: Path = INPUT_PATH,
    output_path: Path = OUTPUT_PATH,
) -> dict[str, Any]:
    """Build probability and confidence diagnostics without fitting a calibrator."""

    predictions = _load_predictions(input_path)
    probability_buckets = _probability_buckets(predictions)
    confidence_buckets = _confidence_buckets(predictions)
    brier_score = _clean(
        math.fsum((item["up_probability"] - item["actual"]) ** 2 for item in predictions)
        / len(predictions)
    )
    log_loss = _clean(
        -math.fsum(
            item["actual"] * math.log(_clip(item["up_probability"]))
            + (1 - item["actual"]) * math.log(_clip(1 - item["up_probability"]))
            for item in predictions
        )
        / len(predictions)
    )
    ece = _clean(
        math.fsum(
            bucket["sample_count"] / len(predictions) * bucket["absolute_calibration_error"]
            for bucket in probability_buckets
        )
    )
    confidence_80 = _confidence_threshold(predictions, 0.8)
    confidence_90 = _confidence_threshold(predictions, 0.9)

    average_confidence = _clean(
        math.fsum(item["confidence"] for item in predictions) / len(predictions)
    )
    accuracy = _clean(math.fsum(item["hit"] for item in predictions) / len(predictions))
    confidence_gap = _clean(average_confidence - accuracy)
    # A 2 percentage-point tolerance avoids labeling tiny sampling noise as material.
    if confidence_gap > 0.02:
        assessment = "overconfident"
    elif confidence_gap < -0.02:
        assessment = "underconfident"
    else:
        assessment = "approximately_calibrated"

    payload: dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "version": "1.0",
        "source": "walk_forward_validation",
        "analysis_only": True,
        "sample_count": len(predictions),
        "probability_bucket_definition": "left-inclusive, right-exclusive; final bucket includes 1.0",
        "confidence_definition": "max(up_probability, 1 - up_probability)",
        "metrics": {
            "brier_score": brier_score,
            "log_loss": log_loss,
            "expected_calibration_error": ece,
            "average_confidence": average_confidence,
            "accuracy": accuracy,
            "confidence_minus_accuracy": confidence_gap,
        },
        "probability_buckets": probability_buckets,
        "confidence_buckets": confidence_buckets,
        "high_confidence_accuracy": {
            "confidence_at_least_80_percent": confidence_80,
            "confidence_at_least_90_percent": confidence_90,
        },
        "calibration_assessment": {
            "status": assessment,
            "overconfident": assessment == "overconfident",
            "underconfident": assessment == "underconfident",
            "rule": "average confidence minus actual accuracy; ±0.02 tolerance",
            "note": "Descriptive out-of-sample analysis only; no calibration model was fitted.",
        },
    }
    _validate_report(payload)
    _write_json_atomic(output_path, payload)
    return payload


def _load_predictions(path: Path) -> list[dict[str, Any]]:
    try:
        with path.open("r", encoding="utf-8") as source:
            report = json.load(source)
    except FileNotFoundError as exc:
        raise ValueError(f"Walk-forward report not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid walk-forward JSON: {path}") from exc
    source_rows = report.get("predictions") if isinstance(report, dict) else None
    if not isinstance(source_rows, list) or not source_rows:
        raise ValueError("Walk-forward report contains no predictions")
    if report.get("total_predictions") != len(source_rows):
        raise ValueError("Walk-forward prediction count is inconsistent")

    predictions: list[dict[str, Any]] = []
    for index, item in enumerate(source_rows, start=1):
        try:
            probability = float(item["up_probability"])
            actual = int(item["actual"])
            predicted = int(item["prediction"])
            hit = item["hit"]
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"Invalid walk-forward prediction #{index}") from exc
        if not math.isfinite(probability) or not 0 <= probability <= 1:
            raise ValueError(f"Invalid probability at prediction #{index}")
        if actual not in (0, 1) or predicted not in (0, 1) or not isinstance(hit, bool):
            raise ValueError(f"Invalid class or hit at prediction #{index}")
        if hit != (actual == predicted):
            raise ValueError(f"Incorrect hit at prediction #{index}")
        predictions.append(
            {
                "actual": actual,
                "prediction": predicted,
                "up_probability": probability,
                "confidence": max(probability, 1 - probability),
                "hit": hit,
            }
        )
    return predictions


def _probability_buckets(predictions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    buckets: list[list[dict[str, Any]]] = [[] for _ in range(10)]
    for item in predictions:
        bucket_index = min(int(item["up_probability"] / PROBABILITY_BUCKET_WIDTH), 9)
        buckets[bucket_index].append(item)
    result = []
    for index, selected in enumerate(buckets):
        lower = index / 10
        upper = (index + 1) / 10
        average_probability = _optional_mean(selected, "up_probability")
        actual_up_rate = _optional_mean(selected, "actual")
        signed_error = (
            _clean(average_probability - actual_up_rate)
            if average_probability is not None and actual_up_rate is not None
            else None
        )
        result.append(
            {
                "label": f"{index * 10}-{(index + 1) * 10}%",
                "lower_bound": lower,
                "upper_bound": upper,
                "upper_bound_inclusive": index == 9,
                "sample_count": len(selected),
                "average_predicted_up_probability": average_probability,
                "actual_up_rate": actual_up_rate,
                "calibration_error": signed_error,
                "absolute_calibration_error": abs(signed_error) if signed_error is not None else None,
            }
        )
    return result


def _confidence_buckets(predictions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result = []
    for index, (lower, upper) in enumerate(CONFIDENCE_BUCKETS):
        selected = [
            item for item in predictions
            if item["confidence"] >= lower
            and (item["confidence"] < upper or (index == len(CONFIDENCE_BUCKETS) - 1 and item["confidence"] <= upper))
        ]
        result.append(
            {
                "label": f"{int(lower * 100)}-{int(upper * 100)}%",
                "lower_bound": lower,
                "upper_bound": upper,
                "upper_bound_inclusive": index == len(CONFIDENCE_BUCKETS) - 1,
                "sample_count": len(selected),
                "average_confidence": _optional_mean(selected, "confidence"),
                "accuracy": _optional_mean(selected, "hit"),
            }
        )
    return result


def _confidence_threshold(predictions: list[dict[str, Any]], threshold: float) -> dict[str, Any]:
    selected = [item for item in predictions if item["confidence"] >= threshold]
    return {
        "threshold": threshold,
        "sample_count": len(selected),
        "average_confidence": _optional_mean(selected, "confidence"),
        "accuracy": _optional_mean(selected, "hit"),
    }


def _optional_mean(rows: list[dict[str, Any]], field: str) -> float | None:
    if not rows:
        return None
    return _clean(math.fsum(float(row[field]) for row in rows) / len(rows))


def _clip(value: float) -> float:
    return min(1 - EPSILON, max(EPSILON, value))


def _clean(value: float) -> float:
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("Calibration statistic is not finite")
    return round(result, 12)


def _validate_report(payload: dict[str, Any]) -> None:
    sample_count = payload["sample_count"]
    probability_buckets = payload["probability_buckets"]
    confidence_buckets = payload["confidence_buckets"]
    if len(probability_buckets) != 10 or sum(item["sample_count"] for item in probability_buckets) != sample_count:
        raise ValueError("Probability bucket counts are inconsistent")
    if len(confidence_buckets) != 5 or sum(item["sample_count"] for item in confidence_buckets) != sample_count:
        raise ValueError("Confidence bucket counts are inconsistent")
    for bucket in (*probability_buckets, *confidence_buckets):
        for key, value in bucket.items():
            if isinstance(value, float) and (not math.isfinite(value) or not -1 <= value <= 1):
                raise ValueError(f"Invalid bucket statistic: {key}")
    metrics = payload["metrics"]
    if not 0 <= metrics["brier_score"] <= 1 or metrics["log_loss"] < 0 or not 0 <= metrics["expected_calibration_error"] <= 1:
        raise ValueError("Invalid calibration metric")


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
    LOGGER.info("Probability calibration analysis started")
    try:
        report = analyze_probability_calibration()
    except (OSError, ValueError) as exc:
        LOGGER.error("Probability calibration analysis failed: %s", exc)
        return 1
    LOGGER.info("Probability calibration report written: %s", OUTPUT_PATH)
    LOGGER.info(
        "Probability calibration finished | samples=%d | brier=%.4f | log_loss=%.4f | ece=%.4f",
        report["sample_count"],
        report["metrics"]["brier_score"],
        report["metrics"]["log_loss"],
        report["metrics"]["expected_calibration_error"],
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
