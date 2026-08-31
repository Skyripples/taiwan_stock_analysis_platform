"""Analyze correlations in the engineered next-day prediction dataset."""

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


LOGGER = logging.getLogger("feature_correlation")
INPUT_PATH = PROJECT_ROOT / "data" / "history" / "engineered_prediction_dataset.csv"
OUTPUT_PATH = PROJECT_ROOT / "data" / "analysis" / "archive" / "feature_correlation.json"

ORIGINAL_FEATURES = (
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

ENGINEERED_FEATURES = (
    "turnover_ratio_20d",
    "market_breadth",
    "advance_decline_ratio",
    "foreign_cash_flow_5d_sum",
    "foreign_cash_flow_20d_zscore",
    "foreign_futures_change_1d",
    "foreign_futures_change_5d",
    "taiex_return_3d",
    "taiex_return_5d",
    "taiex_return_20d",
    "taiex_ma5_distance",
    "taiex_ma20_distance",
    "taiex_volatility_20d",
)

GLOBAL_MACRO_FEATURES = (
    "vix_change_percent",
    "us10y_change",
    "us2y_change",
    "yield_curve_10y_2y",
    "usdtwd_change_percent",
    "dxy_change_percent",
)

FEATURES = (*ORIGINAL_FEATURES, *ENGINEERED_FEATURES, *GLOBAL_MACRO_FEATURES)
TARGETS = ("target_direction", "next_taiex_return")
EXCLUDED_NON_FEATURE_FIELDS = {
    "feature_date",
    "target_date",
    "taiwan_market_trade_date",
    "institutional_trade_date",
    "foreign_futures_trade_date",
    "night_futures_trade_date",
    "tsm_adr_trade_date",
    "sox_trade_date",
    "sp500_trade_date",
    "nasdaq_trade_date",
    "next_taiex_close",
    *TARGETS,
}
HIGH_CORRELATION_THRESHOLD = 0.85


def analyze_feature_correlation(
    input_path: Path = INPUT_PATH,
    output_path: Path = OUTPUT_PATH,
) -> dict[str, Any]:
    """Calculate target correlations, feature matrix, and group comparisons."""

    columns, row_count = _load_numeric_columns(input_path)
    target_correlations = {
        target: {feature: _pearson(columns[feature], columns[target]) for feature in FEATURES}
        for target in TARGETS
    }
    matrix = {
        left: {right: _pearson(columns[left], columns[right]) for right in FEATURES}
        for left in FEATURES
    }
    rankings = {
        target: _rank_correlations(target_correlations[target], 15)
        for target in TARGETS
    }
    high_pairs = [
        {
            "feature_1": left,
            "feature_2": right,
            "correlation": matrix[left][right],
            "absolute_correlation": abs(matrix[left][right]),
        }
        for left_index, left in enumerate(FEATURES)
        for right in FEATURES[left_index + 1 :]
        if abs(matrix[left][right]) >= HIGH_CORRELATION_THRESHOLD
    ]
    high_pairs.sort(key=lambda item: (-item["absolute_correlation"], item["feature_1"], item["feature_2"]))

    payload: dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "version": "1.0",
        "dataset": "engineered_prediction_dataset",
        "sample_count": row_count,
        "method": "pearson",
        "feature_count": len(FEATURES),
        "feature_groups": {
            "original": list(ORIGINAL_FEATURES),
            "engineered": list(ENGINEERED_FEATURES),
            "global_macro": list(GLOBAL_MACRO_FEATURES),
        },
        "excluded_non_feature_fields": sorted(EXCLUDED_NON_FEATURE_FIELDS),
        "target_correlations": target_correlations,
        "top_15": rankings,
        "group_comparison": {
            target: {
                "original": _group_summary(target_correlations[target], ORIGINAL_FEATURES),
                "engineered": _group_summary(target_correlations[target], ENGINEERED_FEATURES),
                "global_macro": _group_summary(target_correlations[target], GLOBAL_MACRO_FEATURES),
            }
            for target in TARGETS
        },
        "feature_correlation_matrix": matrix,
        "high_collinearity_threshold": HIGH_CORRELATION_THRESHOLD,
        "high_collinearity_pairs": high_pairs,
    }
    _validate_payload(payload)
    _write_json_atomic(output_path, payload)
    return payload


def _load_numeric_columns(path: Path) -> tuple[dict[str, list[float]], int]:
    try:
        handle = path.open("r", encoding="utf-8-sig", newline="")
    except FileNotFoundError as exc:
        raise ValueError(f"Engineered prediction dataset not found: {path}") from exc
    required = {*FEATURES, *TARGETS}
    columns = {field: [] for field in required}
    with handle:
        reader = csv.DictReader(handle)
        missing = sorted(required.difference(reader.fieldnames or ()))
        if missing:
            raise ValueError("Engineered dataset is missing columns: " + ", ".join(missing))
        for row_number, row in enumerate(reader, start=2):
            for field in required:
                try:
                    value = float(str(row[field]).strip())
                except (TypeError, ValueError) as exc:
                    raise ValueError(f"Invalid {field} at CSV row {row_number}") from exc
                if not math.isfinite(value):
                    raise ValueError(f"Non-finite {field} at CSV row {row_number}")
                columns[field].append(value)
    row_count = len(columns[TARGETS[0]])
    if row_count < 2:
        raise ValueError("At least two complete samples are required")
    return columns, row_count


def _pearson(left: list[float], right: list[float]) -> float:
    if len(left) != len(right) or len(left) < 2:
        raise ValueError("Correlation inputs must have equal lengths of at least two")
    left_mean = math.fsum(left) / len(left)
    right_mean = math.fsum(right) / len(right)
    covariance = math.fsum((x - left_mean) * (y - right_mean) for x, y in zip(left, right))
    left_ss = math.fsum((x - left_mean) ** 2 for x in left)
    right_ss = math.fsum((y - right_mean) ** 2 for y in right)
    denominator = math.sqrt(left_ss * right_ss)
    if denominator == 0:
        raise ValueError("Correlation is undefined for a constant column")
    # Clamp only floating-point noise outside the mathematical range.
    return round(max(-1.0, min(1.0, covariance / denominator)), 12)


def _rank_correlations(correlations: dict[str, float], limit: int) -> list[dict[str, Any]]:
    ranked = sorted(correlations.items(), key=lambda item: (-abs(item[1]), item[0]))
    return [
        {"rank": index, "feature": feature, "correlation": value, "absolute_correlation": abs(value)}
        for index, (feature, value) in enumerate(ranked[:limit], start=1)
    ]


def _group_summary(correlations: dict[str, float], features: Iterable[str]) -> dict[str, Any]:
    selected = {feature: correlations[feature] for feature in features}
    ranked = _rank_correlations(selected, len(selected))
    return {
        "feature_count": len(selected),
        "mean_absolute_correlation": round(math.fsum(abs(value) for value in selected.values()) / len(selected), 12),
        "max_absolute_correlation": ranked[0]["absolute_correlation"],
        "top_features": ranked,
    }


def _validate_payload(payload: dict[str, Any]) -> None:
    matrix = payload["feature_correlation_matrix"]
    for left in FEATURES:
        if set(matrix[left]) != set(FEATURES):
            raise ValueError(f"Incomplete correlation matrix row: {left}")
        for right in FEATURES:
            value = matrix[left][right]
            if not math.isfinite(value) or not -1 <= value <= 1:
                raise ValueError(f"Invalid correlation: {left} / {right}")
            if value != matrix[right][left]:
                raise ValueError(f"Asymmetric correlation matrix: {left} / {right}")
    pairs = payload["high_collinearity_pairs"]
    identities = {(item["feature_1"], item["feature_2"]) for item in pairs}
    if len(identities) != len(pairs):
        raise ValueError("Duplicate high-collinearity pair")


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
    LOGGER.info("Feature correlation analysis started")
    try:
        report = analyze_feature_correlation()
    except (OSError, ValueError) as exc:
        LOGGER.error("Feature correlation analysis failed: %s", exc)
        return 1
    LOGGER.info("Feature correlation report written: %s", OUTPUT_PATH)
    LOGGER.info(
        "Feature correlation analysis finished | samples=%d | features=%d | high_pairs=%d",
        report["sample_count"],
        report["feature_count"],
        len(report["high_collinearity_pairs"]),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
