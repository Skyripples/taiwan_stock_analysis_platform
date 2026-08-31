"""Correlation and walk-forward validation for semiconductor candidates."""

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
from research.validate_global_macro_features import _metric_delta, _walk_forward


LOGGER = logging.getLogger("semiconductor_validation")
INPUT_PATH = PROJECT_ROOT / "data" / "history" / "historical_prediction_dataset.csv"
OUTPUT_PATH = PROJECT_ROOT / "data" / "analysis" / "archive" / "semiconductor_feature_validation.json"
INITIAL_TRAINING_SIZE = 250
FORMAL_FEATURES = (
    "taiex_close", "taiex_change_percent", "tpex_close", "turnover",
    "advancing", "declining", "unchanged", "foreign_cash_flow",
    "foreign_futures_position", "night_futures_change",
    "tsm_adr_change_percent", "sox_change_percent", "sp500_change_percent",
    "nasdaq_change_percent", "vix_change_percent", "kospi_change_percent",
)
CANDIDATES = (
    "soxx_change_percent", "smh_change_percent", "nvda_change_percent",
    "amd_change_percent", "avgo_change_percent",
)
COMPARISON_FEATURES = (
    "sox_change_percent", "tsm_adr_change_percent", "nasdaq_change_percent",
)


def validate_semiconductor_features() -> dict[str, Any]:
    rows = _load_rows()
    feature_sets = {
        "A_formal_16": FORMAL_FEATURES,
        "B_16_plus_all_semiconductor": (*FORMAL_FEATURES, *CANDIDATES),
        "C_16_plus_soxx": (*FORMAL_FEATURES, CANDIDATES[0]),
        "D_16_plus_smh": (*FORMAL_FEATURES, CANDIDATES[1]),
        "E_16_plus_nvda": (*FORMAL_FEATURES, CANDIDATES[2]),
        "F_16_plus_amd": (*FORMAL_FEATURES, CANDIDATES[3]),
        "G_16_plus_avgo": (*FORMAL_FEATURES, CANDIDATES[4]),
    }
    models = {name: _walk_forward(rows, features) for name, features in feature_sets.items()}
    baseline = models["A_formal_16"]
    correlations = {
        feature: {
            "target_direction": _correlation(rows, feature, "target_direction"),
            "next_taiex_return": _correlation(rows, feature, "next_taiex_return"),
            "existing_semiconductor_and_nasdaq": {
                other: _correlation(rows, feature, other) for other in COMPARISON_FEATURES
            },
        }
        for feature in CANDIDATES
    }
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "version": "1.0",
        "dataset": "historical_prediction_dataset",
        "source": "Yahoo Finance daily completed candles",
        "timing_rule": "latest completed U.S. session strictly before target Taiwan trading date",
        "initial_training_size": INITIAL_TRAINING_SIZE,
        "sample_count": len(rows),
        "prediction_count_per_experiment": len(rows) - INITIAL_TRAINING_SIZE,
        "correlations": correlations,
        "models": models,
        "deltas_vs_A": {
            name: _metric_delta(result, baseline)
            for name, result in models.items() if name != "A_formal_16"
        },
        "production_model_modified": False,
    }
    _validate(payload)
    _write_atomic(payload)
    return payload


def _load_rows() -> list[dict[str, Any]]:
    required = {
        "feature_date", "target_date", "target_direction", "next_taiex_return",
        *FORMAL_FEATURES, *CANDIDATES,
    }
    with INPUT_PATH.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        missing = sorted(required.difference(reader.fieldnames or ()))
        if missing:
            raise ValueError("Historical dataset is missing columns: " + ", ".join(missing))
        rows: list[dict[str, Any]] = []
        previous = ""
        numeric = (*FORMAL_FEATURES, *CANDIDATES, "next_taiex_return")
        for number, source in enumerate(reader, 2):
            feature_date, target_date = source["feature_date"], source["target_date"]
            if target_date <= feature_date or (previous and feature_date <= previous):
                raise ValueError(f"Invalid chronological dates at row {number}")
            values = {field: float(source[field]) for field in numeric}
            target = int(source["target_direction"])
            if target not in (0, 1) or not all(math.isfinite(value) for value in values.values()):
                raise ValueError(f"Invalid target or feature at row {number}")
            rows.append({
                "feature_date": feature_date, "target_date": target_date,
                "target_direction": target, **values,
            })
            previous = feature_date
    if len(rows) <= INITIAL_TRAINING_SIZE:
        raise ValueError("Historical dataset is too short")
    return rows


def _correlation(rows: list[dict[str, Any]], left: str, right: str) -> float:
    x, y = [float(row[left]) for row in rows], [float(row[right]) for row in rows]
    x_mean, y_mean = sum(x) / len(x), sum(y) / len(y)
    numerator = sum((a - x_mean) * (b - y_mean) for a, b in zip(x, y))
    denominator = math.sqrt(sum((a - x_mean) ** 2 for a in x) * sum((b - y_mean) ** 2 for b in y))
    if denominator == 0: raise ValueError(f"Undefined correlation: {left} vs {right}")
    return round(numerator / denominator, 12)


def _validate(payload: dict[str, Any]) -> None:
    expected = payload["prediction_count_per_experiment"]
    for result in payload["models"].values():
        if result["prediction_count"] != expected:
            raise ValueError("Experiments do not use identical OOS dates")
    for values in payload["correlations"].values():
        all_values = [values["target_direction"], values["next_taiex_return"], *values["existing_semiconductor_and_nasdaq"].values()]
        if not all(-1 <= value <= 1 for value in all_values):
            raise ValueError("Correlation outside [-1, 1]")


def _write_atomic(payload: dict[str, Any]) -> None:
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
        report = validate_semiconductor_features()
    except (OSError, RuntimeError, ValueError) as exc:
        LOGGER.error("Semiconductor validation failed: %s", exc)
        return 1
    LOGGER.info("Semiconductor validation completed | experiments=7 | predictions=%d", report["prediction_count_per_experiment"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
