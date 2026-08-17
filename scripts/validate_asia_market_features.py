"""Correlation and walk-forward validation for candidate Asian market features."""

from __future__ import annotations

import csv
import json
import logging
import math
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from config import PROJECT_ROOT
from validate_global_macro_features import _metric_delta, _walk_forward


LOGGER = logging.getLogger("asia_market_validation")
INPUT_PATH = PROJECT_ROOT / "data" / "history" / "historical_prediction_dataset.csv"
OUTPUT_PATH = PROJECT_ROOT / "data" / "analysis" / "asia_market_validation.json"
INITIAL_TRAINING_SIZE = 250
FORMAL_FEATURES = (
    "taiex_close", "taiex_change_percent", "tpex_close", "turnover",
    "advancing", "declining", "unchanged", "foreign_cash_flow",
    "foreign_futures_position", "night_futures_change",
    "tsm_adr_change_percent", "sox_change_percent",
    "sp500_change_percent", "nasdaq_change_percent", "vix_change_percent",
)
ASIA_FEATURES = (
    "nikkei_change_percent", "kospi_change_percent",
    "hang_seng_change_percent", "csi300_change_percent",
)
US_COMPARISON = (
    "sp500_change_percent", "nasdaq_change_percent",
    "sox_change_percent", "tsm_adr_change_percent",
)


def validate_asia_market_features() -> dict[str, Any]:
    rows = _load_rows()
    experiments = {
        "A_formal_15": FORMAL_FEATURES,
        "B_15_plus_all_asia": (*FORMAL_FEATURES, *ASIA_FEATURES),
        "C_15_plus_nikkei": (*FORMAL_FEATURES, ASIA_FEATURES[0]),
        "D_15_plus_kospi": (*FORMAL_FEATURES, ASIA_FEATURES[1]),
        "E_15_plus_hang_seng": (*FORMAL_FEATURES, ASIA_FEATURES[2]),
        "F_15_plus_csi300": (*FORMAL_FEATURES, ASIA_FEATURES[3]),
    }
    models = {name: _walk_forward(rows, features) for name, features in experiments.items()}
    baseline = models["A_formal_15"]
    correlations = {
        feature: {
            "target_direction": _correlation(rows, feature, "target_direction"),
            "next_taiex_return": _correlation(rows, feature, "next_taiex_return"),
            "existing_international": {
                other: _correlation(rows, feature, other) for other in US_COMPARISON
            },
        }
        for feature in ASIA_FEATURES
    }
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "version": "1.0",
        "dataset": "historical_prediction_dataset",
        "source": "Yahoo Finance daily completed candles",
        "timing_rule": "latest completed source session strictly before target Taiwan trading date",
        "initial_training_size": INITIAL_TRAINING_SIZE,
        "sample_count": len(rows),
        "prediction_count_per_experiment": len(rows) - INITIAL_TRAINING_SIZE,
        "correlations": correlations,
        "models": models,
        "deltas_vs_A": {
            name: _metric_delta(result, baseline)
            for name, result in models.items() if name != "A_formal_15"
        },
        "production_model_modified": False,
    }
    _validate(payload)
    _write(payload)
    return payload


def _load_rows() -> list[dict[str, Any]]:
    required = {
        "feature_date", "target_date", "target_direction", "next_taiex_return",
        *FORMAL_FEATURES, *ASIA_FEATURES,
    }
    with INPUT_PATH.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        missing = sorted(required.difference(reader.fieldnames or ()))
        if missing:
            raise ValueError("Historical dataset is missing columns: " + ", ".join(missing))
        rows: list[dict[str, Any]] = []
        previous = ""
        numeric = (*FORMAL_FEATURES, *ASIA_FEATURES, "next_taiex_return")
        for number, source in enumerate(reader, 2):
            if source["target_date"] <= source["feature_date"] or (previous and source["feature_date"] <= previous):
                raise ValueError(f"Invalid date order at row {number}")
            values = {field: float(source[field]) for field in numeric}
            target = int(source["target_direction"])
            if target not in (0, 1) or not all(math.isfinite(value) for value in values.values()):
                raise ValueError(f"Invalid value at row {number}")
            rows.append({
                "feature_date": source["feature_date"], "target_date": source["target_date"],
                "target_direction": target, **values,
            })
            previous = source["feature_date"]
    return rows


def _correlation(rows: list[dict[str, Any]], left: str, right: str) -> float:
    left_values = [float(row[left]) for row in rows]
    right_values = [float(row[right]) for row in rows]
    left_mean = sum(left_values) / len(left_values)
    right_mean = sum(right_values) / len(right_values)
    numerator = sum((x - left_mean) * (y - right_mean) for x, y in zip(left_values, right_values))
    denominator = math.sqrt(
        sum((x - left_mean) ** 2 for x in left_values)
        * sum((y - right_mean) ** 2 for y in right_values)
    )
    if denominator == 0:
        raise ValueError(f"Undefined correlation: {left} vs {right}")
    return round(numerator / denominator, 12)


def _validate(payload: dict[str, Any]) -> None:
    expected = payload["prediction_count_per_experiment"]
    for result in payload["models"].values():
        if result["prediction_count"] != expected:
            raise ValueError("Experiments do not use identical dates")
    for values in payload["correlations"].values():
        correlations = [values["target_direction"], values["next_taiex_return"], *values["existing_international"].values()]
        if not all(-1 <= value <= 1 for value in correlations):
            raise ValueError("Correlation outside [-1, 1]")


def _write(payload: dict[str, Any]) -> None:
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
        report = validate_asia_market_features()
    except (OSError, RuntimeError, ValueError) as exc:
        LOGGER.error("Asia market validation failed: %s", exc)
        return 1
    LOGGER.info("Asia market validation completed | experiments=6 | predictions=%d", report["prediction_count_per_experiment"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
