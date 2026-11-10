"""Run reproducible, availability-safe B4 baseline models without tuning.

This is intentionally a benchmark runner, not a signal generator.  Every
model and its preprocessing are recreated and fitted inside each expanding
walk-forward fold.  It never rewrites the B4 dataset or an existing report.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import warnings
from collections import defaultdict
from datetime import date, datetime, time
from pathlib import Path
from typing import Any, Iterable, Mapping
from zoneinfo import ZoneInfo

import numpy as np

from config import PROJECT_ROOT
from research.build_futures_b4_same_day_dataset import (
    BINARY_COLUMN, FEATURE_COLUMNS, OUTPUT_ROOT, VERSION as DATASET_VERSION,
)
from research.futures_binary.walk_forward import expanding_walk_forward_prepared
from research.futures_b4_streak import STREAK_COLUMNS, attach_streak_features
from research.futures_binary.features import feature_names


VERSION = "v5.b4-baselines-1.0"
TAIPEI = ZoneInfo("Asia/Taipei")
DOWN, UP = 0, 1
DEFAULT_OUTPUT = PROJECT_ROOT / "reports" / "futures_b4_baselines.json"
DAILY_FEATURE_ROOT = PROJECT_ROOT / "data_lake" / "futures_prediction" / "daily_features"
GLOBAL_LEVEL_COLUMNS = ("sox", "tsm_adr", "nasdaq", "sp500", "usdtwd")
GLOBAL_RETURN_COLUMNS = tuple(f"{name}_return" for name in GLOBAL_LEVEL_COLUMNS)
INSTITUTIONAL_COLUMNS = feature_names(("institutional",))
PCR_COLUMNS = feature_names(("pcr",))
POSITIONING_COLUMNS = (*INSTITUTIONAL_COLUMNS, *PCR_COLUMNS)


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=OUTPUT_ROOT)
    parser.add_argument("--daily-features", type=Path, default=DAILY_FEATURE_ROOT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--experiment", choices=("core", "core-plus-global", "comparison", "core-plus-streak", "core-vs-streak", "positioning-comparison", "positioning-coverage-guard"), default="comparison")
    parser.add_argument("--original-report", type=Path,
                        default=PROJECT_ROOT / "reports" / "futures_b4_positioning_comparison_v5_1.json")
    parser.add_argument("--initial-train-size", type=int, default=504)
    parser.add_argument("--test-size", type=int, default=21)
    return parser.parse_args()


def _aware(value: Any, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value


def _validate_row(row: Mapping[str, Any], *, expected_time: time = time(8, 30),
                  allow_resolved_flat: bool = False, task_label: str = "B4",
                  feature_available_field: str = "feature_available") -> None:
    if type(row.get("prediction_date")) is not date:
        raise ValueError("prediction_date must be date")
    prediction = _aware(row.get("prediction_at"), "prediction_at")
    local = prediction.astimezone(TAIPEI)
    if local.date() != row["prediction_date"] or local.time() != expected_time:
        raise ValueError(f"{task_label} prediction_at must be {expected_time.strftime('%H:%M')} Asia/Taipei")
    target = row.get(BINARY_COLUMN)
    if target not in {DOWN, UP, None}:
        raise ValueError("B4 binary target must be DOWN=0, UP=1, or None")
    target_at = row.get("target_available_at")
    resolved_flat = allow_resolved_flat and row.get("direction_class") in {"flat", "FLAT"}
    if target is None:
        if target_at is not None and not resolved_flat:
            raise ValueError("Missing target must not have target_available_at")
    elif _aware(target_at, "target_available_at") <= prediction:
        raise ValueError("B4 target was available at or before prediction_at")
    if not isinstance(row.get(feature_available_field), bool):
        raise ValueError(f"{feature_available_field} must be boolean")


def load_b4_rows(root: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Read B4 Parquet rows; no data is modified or imputed at this boundary."""
    import pyarrow.parquet as pq

    paths = sorted(root.rglob("*.parquet"))
    if not paths:
        raise FileNotFoundError(f"No B4 Parquet files: {root}")
    schema = None
    rows: list[dict[str, Any]] = []
    identity_files: list[dict[str, Any]] = []
    for path in paths:
        table = pq.ParquetFile(path).read()
        if schema is not None and not table.schema.equals(schema, check_metadata=False):
            raise ValueError(f"B4 Parquet schema mismatch: {path}")
        schema = table.schema
        rows.extend(table.to_pylist())
        identity_files.append({
            "path": str(path.relative_to(root)).replace("\\", "/"),
            "bytes": path.stat().st_size,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        })
    for row in rows:
        _validate_row(row)
    dates = [row["prediction_date"] for row in rows]
    if len(dates) != len(set(dates)):
        raise ValueError("Duplicate B4 prediction_date")
    rows.sort(key=lambda row: row["prediction_at"])
    return rows, {
        "dataset_version": DATASET_VERSION,
        "files": identity_files,
        "rows": len(rows),
        "schema": str(schema),
    }


def _candidate_rows(rows: list[dict[str, Any]], feature_columns: tuple[str, ...], *,
                    feature_available_field: str = "feature_available") -> tuple[list[tuple[int, dict[str, Any]]], list[dict[str, Any]]]:
    indexed: list[tuple[int, dict[str, Any]]] = []
    excluded: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        if not row[feature_available_field]:
            excluded.append({
                "index": index, "prediction_date": row["prediction_date"].isoformat(),
                "reason": row.get("feature_missing_reason") or "feature_unavailable",
            })
            continue
        missing = [name for name in feature_columns if name not in row]
        if missing:
            raise ValueError(f"B4 candidate lacks allowlisted features: {missing}")
        indexed.append((index, row))
    return indexed, excluded


def _matrix(rows: Iterable[Mapping[str, Any]], feature_columns: tuple[str, ...]) -> np.ndarray:
    result = []
    for row in rows:
        values = []
        for name in feature_columns:
            value = row[name]
            if value is None:
                values.append(np.nan)
            elif isinstance(value, bool):
                values.append(float(value))
            elif isinstance(value, (int, float)) and np.isfinite(value):
                values.append(float(value))
            else:
                raise ValueError(f"Invalid B4 feature {name}")
        result.append(values)
    return np.asarray(result, dtype=float)


def _preprocessor() -> Any:
    from sklearn.impute import SimpleImputer
    from sklearn.pipeline import Pipeline
    from sklearn.preprocessing import StandardScaler

    # keep_empty_features makes an all-NaN training column deterministic: it
    # remains represented (filled with zero) and receives a missing indicator.
    return Pipeline([
        ("imputer", SimpleImputer(strategy="median", add_indicator=True, keep_empty_features=True)),
        ("scaler", StandardScaler()),
    ])


def _models() -> dict[str, Any]:
    from sklearn.dummy import DummyClassifier
    from sklearn.ensemble import HistGradientBoostingClassifier
    from sklearn.linear_model import LogisticRegression

    return {
        "dummy": DummyClassifier(strategy="prior", random_state=0),
        "logistic": LogisticRegression(C=1.0, max_iter=1000, solver="lbfgs", random_state=0),
        "hist_gradient_boosting": HistGradientBoostingClassifier(
            learning_rate=0.05, max_iter=100, max_leaf_nodes=15,
            l2_regularization=1.0, early_stopping=False, random_state=0,
        ),
    }


def _probability_up(model: Any, transformed: np.ndarray) -> np.ndarray:
    probabilities = model.predict_proba(transformed)
    classes = list(model.classes_)
    if UP not in classes:
        return np.zeros(len(transformed), dtype=float)
    return probabilities[:, classes.index(UP)]


def _metric_bundle(records: list[dict[str, Any]]) -> dict[str, Any]:
    from sklearn.metrics import (
        accuracy_score, balanced_accuracy_score, brier_score_loss, confusion_matrix,
        f1_score, log_loss, precision_recall_fscore_support, roc_auc_score,
    )

    observed = [item for item in records if item["target"] is not None and item["prediction"] is not None]
    if not observed:
        return {"support": 0, "values": {}, "unavailable": {"all": "no observed labels or model predictions"}}
    truth = np.asarray([item["target"] for item in observed], dtype=int)
    predicted = np.asarray([item["prediction"] for item in observed], dtype=int)
    probabilities = np.asarray([item["probability_up"] for item in observed], dtype=float)
    precision, recall, f1, support = precision_recall_fscore_support(
        truth, predicted, labels=[DOWN, UP], zero_division=0,
    )
    values: dict[str, Any] = {
        "accuracy": float(accuracy_score(truth, predicted)),
        "down": {"precision": float(precision[0]), "recall": float(recall[0]), "f1": float(f1[0]), "support": int(support[0])},
        "up": {"precision": float(precision[1]), "recall": float(recall[1]), "f1": float(f1[1]), "support": int(support[1])},
        "macro_f1": float(f1_score(truth, predicted, labels=[DOWN, UP], average="macro", zero_division=0)),
        "confusion_matrix": confusion_matrix(truth, predicted, labels=[DOWN, UP]).tolist(),
        "log_loss": float(log_loss(truth, probabilities, labels=[DOWN, UP])),
        "brier": float(brier_score_loss(truth, probabilities)),
    }
    unavailable: dict[str, str] = {}
    if len(set(truth)) < 2:
        unavailable["balanced_accuracy"] = "requires both DOWN=0 and UP=1 labels"
        unavailable["roc_auc"] = "requires both DOWN=0 and UP=1 labels"
    else:
        values["balanced_accuracy"] = float(balanced_accuracy_score(truth, predicted))
        values["roc_auc"] = float(roc_auc_score(truth, probabilities))
    return {"support": int(len(observed)), "values": values, "unavailable": unavailable}


def _fold_model_records(name: str, model: Any, train_rows: list[dict[str, Any]],
                        test_rows: list[dict[str, Any]], fold_number: int,
                        feature_columns: tuple[str, ...]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    train_targets = np.asarray([row[BINARY_COLUMN] for row in train_rows], dtype=int)
    base = [{
        "fold": fold_number, "prediction_date": row["prediction_date"].isoformat(),
        "target": row[BINARY_COLUMN], "direction_class": row.get("direction_class"),
        "score_exclusion_reason": (
            "flat_label_excluded_from_binary" if row.get("direction_class") in {"flat", "FLAT"}
            else "missing_label" if row[BINARY_COLUMN] is None else None
        ),
        "probability_up": None, "prediction": None,
    } for row in test_rows]
    if name != "dummy" and len(set(train_targets)) < 2:
        return base, {"reason": "training fold contains one class"}
    warnings_seen: list[str] = []
    try:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            if name == "dummy":
                transformed_train = _matrix(train_rows, feature_columns)
                transformed_test = _matrix(test_rows, feature_columns)
                all_missing: list[str] = []
            else:
                transformer = _preprocessor()
                raw_train = _matrix(train_rows, feature_columns)
                transformed_train = transformer.fit_transform(raw_train)
                transformed_test = transformer.transform(_matrix(test_rows, feature_columns))
                all_missing = [feature_columns[index] for index in range(len(feature_columns)) if np.isnan(raw_train[:, index]).all()]
            model.fit(transformed_train, train_targets)
        warnings_seen = [f"{item.category.__name__}: {item.message}" for item in caught]
        probability = _probability_up(model, transformed_test)
        predicted = model.predict(transformed_test).astype(int)
        for item, prob, label in zip(base, probability, predicted):
            item["probability_up"] = float(prob)
            item["prediction"] = int(label)
        return base, {"all_missing_features": all_missing, "warnings": warnings_seen}
    except Exception as error:
        return base, {"reason": f"fit_or_predict_failed: {type(error).__name__}: {error}", "warnings": warnings_seen}


def _training_feature_diagnostics(train_rows: list[dict[str, Any]], feature_columns: tuple[str, ...]) -> dict[str, Any]:
    matrix = _matrix(train_rows, feature_columns)
    coverage = {
        name: {"non_null": int((~np.isnan(matrix[:, index])).sum()), "coverage": float((~np.isnan(matrix[:, index])).mean())}
        for index, name in enumerate(feature_columns)
    }
    return {"training_rows": len(train_rows), "coverage": coverage,
            "all_missing_features": [name for name, value in coverage.items() if value["non_null"] == 0]}


def run_rows(rows: list[dict[str, Any]], *, initial_train_size: int = 504, test_size: int = 21,
             data_identity: dict[str, Any] | None = None,
             feature_columns: tuple[str, ...] = FEATURE_COLUMNS,
             experiment_name: str = "tx_core", expected_time: time = time(8, 30),
             feature_available_field: str = "feature_available", allow_resolved_flat: bool = False,
             task_name: str = "B4 same-day TX regular close/open") -> dict[str, Any]:
    """Execute all fixed baselines on supplied B4 rows; designed for fixtures too."""
    for row in rows:
        _validate_row(row, expected_time=expected_time, allow_resolved_flat=allow_resolved_flat,
                      task_label=task_name, feature_available_field=feature_available_field)
    feature_columns = tuple(feature_columns)
    indexed, excluded = _candidate_rows(rows, feature_columns, feature_available_field=feature_available_field)
    folds = expanding_walk_forward_prepared(indexed, initial_train_size=initial_train_size, test_size=test_size,
                                             allow_resolved_flat=allow_resolved_flat)
    model_records: dict[str, list[dict[str, Any]]] = defaultdict(list)
    fold_reports: list[dict[str, Any]] = []
    for number, fold in enumerate(folds, start=1):
        by_index = {index: row for index, row in indexed}
        train_rows = [by_index[index] for index in fold.train_indices]
        test_rows = [by_index[index] for index in fold.test_indices]
        fold_report: dict[str, Any] = {
            "fold": number, "fit_at": fold.fit_at.isoformat(), "train_count": len(train_rows),
            "test_count": len(test_rows), "purged_count": len(fold.purged_indices),
            "train_indices": list(fold.train_indices), "test_indices": list(fold.test_indices),
            "purged_indices": list(fold.purged_indices),
            "test_start": test_rows[0]["prediction_date"].isoformat(),
            "test_end": test_rows[-1]["prediction_date"].isoformat(), "models": {},
            "training_feature_diagnostics": _training_feature_diagnostics(train_rows, feature_columns),
        }
        for name, model in _models().items():
            records, diagnostics = _fold_model_records(name, model, train_rows, test_rows, number, feature_columns)
            model_records[name].extend(records)
            fold_report["models"][name] = {"diagnostics": diagnostics, "metrics": _metric_bundle(records)}
        fold_reports.append(fold_report)
    models_report: dict[str, Any] = {}
    for name, records in model_records.items():
        by_year: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for record in records:
            by_year[record["prediction_date"][:4]].append(record)
        models_report[name] = {
            "oos_predictions": records,
            "overall_metrics": _metric_bundle(records),
            "year_metrics": {year: _metric_bundle(items) for year, items in sorted(by_year.items())},
        }
    scored_by_model = {
        name: {record["prediction_date"] for record in records
               if record["target"] is not None and record["prediction"] is not None}
        for name, records in model_records.items()
    }
    common_scored = set.intersection(*scored_by_model.values()) if scored_by_model else set()
    return {
        "version": VERSION,
        "runtime": {"sklearn_version": __import__("sklearn").__version__},
        "experiment": experiment_name,
        "task": {"name": task_name, "labels": ({"DOWN": DOWN, "UP": UP, "FLAT": "excluded from binary scoring"}
                                                     if allow_resolved_flat else {"DOWN": DOWN, "UP": UP})},
        "dataset": data_identity or {"rows": len(rows)},
        "feature_columns": list(feature_columns),
        "model_settings": {
            "dummy": {"strategy": "prior", "prediction": "training-fold majority class"},
            "logistic": {"C": 1.0, "max_iter": 1000, "solver": "lbfgs", "preprocessing": "train-only median imputer + missing indicators + scaler"},
            "hist_gradient_boosting": {"learning_rate": 0.05, "max_iter": 100, "max_leaf_nodes": 15, "l2_regularization": 1.0, "early_stopping": False, "preprocessing": "train-only median imputer + missing indicators + scaler"},
        },
        "row_counts": {
            "dataset": len(rows), "model_candidates": len(indexed), "excluded_feature_unavailable": len(excluded),
            "test_sessions": sum(len(fold.test_indices) for fold in folds),
            "scorable_by_model": {name: len(days) for name, days in scored_by_model.items()},
            "actual_predictions_by_model": {name: sum(record["prediction"] is not None for record in records)
                                            for name, records in model_records.items()},
            "common_scorable_dates": sorted(common_scored),
            "common_scorable_count": len(common_scored),
        },
        "excluded_rows": excluded,
        "folds": fold_reports,
        "models": models_report,
    }


def load_daily_pit_rows(root: Path) -> tuple[dict[date, dict[str, Any]], dict[str, Any]]:
    """Read PIT feature rows needed for global returns without rebuilding them."""
    import pyarrow.parquet as pq

    paths = sorted(root.rglob("*.parquet"))
    if not paths:
        raise FileNotFoundError(f"No daily PIT feature Parquet files: {root}")
    required = {"prediction_date", "prediction_at"}
    for prefix in GLOBAL_LEVEL_COLUMNS:
        required.update({f"{prefix}_value", f"{prefix}_source_date", f"{prefix}_available_at"})
    for prefix in ("foreign", "investment_trust", "dealer"):
        required.update({f"{prefix}_available_at", *[name for name in INSTITUTIONAL_COLUMNS if name.startswith(f"{prefix}_")]})
    required.update({"pcr_available_at", *PCR_COLUMNS})
    rows: dict[date, dict[str, Any]] = {}
    identity_files = []
    for path in paths:
        table = pq.ParquetFile(path).read(columns=sorted(required))
        missing = required - set(table.schema.names)
        if missing:
            raise ValueError(f"Daily PIT feature schema lacks global provenance: {sorted(missing)}")
        for row in table.to_pylist():
            day = row["prediction_date"]
            if day in rows:
                raise ValueError(f"Duplicate daily PIT prediction_date: {day}")
            _aware(row["prediction_at"], "daily PIT prediction_at")
            rows[day] = row
        identity_files.append({
            "path": str(path.relative_to(root)).replace("\\", "/"),
            "bytes": path.stat().st_size,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        })
    return rows, {"root": str(root), "files": identity_files, "rows": len(rows)}


def derive_global_returns(pit_rows: Iterable[Mapping[str, Any]]) -> dict[str, dict[date, dict[str, Any]]]:
    """Derive adjacent *source-observation* returns, never calendar-row returns."""
    observations: dict[str, dict[date, dict[str, Any]]] = {prefix: {} for prefix in GLOBAL_LEVEL_COLUMNS}
    for row in pit_rows:
        for prefix in GLOBAL_LEVEL_COLUMNS:
            value = row.get(f"{prefix}_value")
            source_date = row.get(f"{prefix}_source_date")
            available_at = row.get(f"{prefix}_available_at")
            if value is None or source_date is None or available_at is None:
                continue
            if type(source_date) is not date or not isinstance(value, (int, float)) or not np.isfinite(value):
                raise ValueError(f"Invalid {prefix} source observation")
            available = _aware(available_at, f"{prefix}_available_at")
            existing = observations[prefix].get(source_date)
            if existing and float(existing["value"]) != float(value):
                raise ValueError(f"Conflicting {prefix} values for source_date {source_date}")
            if existing and available != existing["available_at"]:
                raise ValueError(f"Conflicting {prefix} availability for source_date {source_date}")
            if existing is None:
                observations[prefix][source_date] = {"value": float(value), "available_at": available}
    derived: dict[str, dict[date, dict[str, Any]]] = {prefix: {} for prefix in GLOBAL_LEVEL_COLUMNS}
    for prefix, values in observations.items():
        prior: dict[str, Any] | None = None
        for source_date in sorted(values):
            current = values[source_date]
            if prior is not None and prior["value"] != 0:
                derived[prefix][source_date] = {
                    "value": current["value"] / prior["value"] - 1.0,
                    "source_date": source_date,
                    "previous_source_date": prior["source_date"],
                    "available_at": max(current["available_at"], prior["available_at"]),
                }
            current["source_date"] = source_date
            prior = current
    return derived


def attach_global_returns(b4_rows: list[dict[str, Any]], pit_rows: Mapping[date, Mapping[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Attach only returns whose two source observations were available at row prediction_at."""
    derived = derive_global_returns(pit_rows.values())
    output: list[dict[str, Any]] = []
    coverage = {name: 0 for name in GLOBAL_RETURN_COLUMNS}
    for source in b4_rows:
        row = dict(source)
        pit = pit_rows.get(row["prediction_date"])
        cutoff = _aware(row["prediction_at"], "prediction_at")
        for prefix, name in zip(GLOBAL_LEVEL_COLUMNS, GLOBAL_RETURN_COLUMNS):
            row[name] = None
            if pit is None:
                continue
            source_date = pit.get(f"{prefix}_source_date")
            observation = derived[prefix].get(source_date)
            if observation is None:
                continue
            if observation["available_at"] > cutoff:
                raise ValueError(f"Derived {name} exceeds prediction_at: {row['prediction_date']}")
            row[name] = observation["value"]
            coverage[name] += 1
        output.append(row)
    definitions = {
        name: {
            "formula": "current source-observation level / immediately prior source-observation level - 1",
            "availability": "max(current observation available_at, prior observation available_at)",
            "alignment": "selected only when derived availability <= row prediction_at",
            "source_level": prefix,
        }
        for prefix, name in zip(GLOBAL_LEVEL_COLUMNS, GLOBAL_RETURN_COLUMNS)
    }
    return output, {"coverage": coverage, "definitions": definitions, "dgs10": "excluded/quarantined"}


def attach_positioning_features(b4_rows: list[dict[str, Any]], pit_rows: Mapping[date, Mapping[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Attach institutional/PCR fields only when their own PIT timestamp is safe for row prediction_at."""
    availability_by_column = {
        **{column: "foreign_available_at" for column in INSTITUTIONAL_COLUMNS if column.startswith("foreign_")},
        **{column: "investment_trust_available_at" for column in INSTITUTIONAL_COLUMNS if column.startswith("investment_trust_")},
        **{column: "dealer_available_at" for column in INSTITUTIONAL_COLUMNS if column.startswith("dealer_")},
        **{column: "pcr_available_at" for column in PCR_COLUMNS},
    }
    output: list[dict[str, Any]] = []
    coverage = {column: 0 for column in POSITIONING_COLUMNS}
    for source in b4_rows:
        row = dict(source)
        pit = pit_rows.get(row["prediction_date"])
        cutoff = _aware(row["prediction_at"], "prediction_at")
        for column in POSITIONING_COLUMNS:
            row[column] = None
            if pit is None or pit.get(column) is None:
                continue
            available = pit.get(availability_by_column[column])
            if available is None:
                raise ValueError(f"{column} has value without source available_at")
            available = _aware(available, availability_by_column[column])
            if available > cutoff:
                raise ValueError(f"{column} exceeds prediction_at: {row['prediction_date']}")
            value = pit[column]
            if not isinstance(value, (int, float)) or not np.isfinite(value):
                raise ValueError(f"Invalid PIT value for {column}")
            row[column] = float(value)
            coverage[column] += 1
        output.append(row)
    metadata = {
        "institutional_columns": list(INSTITUTIONAL_COLUMNS), "pcr_columns": list(PCR_COLUMNS),
        "availability": {
            "institutional": "per-investor canonical available_at <= row prediction_at",
            "pcr": "canonical pcr_available_at <= row prediction_at",
        },
        "coverage": coverage,
        "dgs10": "excluded/quarantined",
        "future_fill": "forbidden; a future available_at raises validation error",
    }
    return output, metadata


def _metric_deltas(core: Mapping[str, Any], global_: Mapping[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for metric in ("accuracy", "balanced_accuracy", "roc_auc", "brier", "log_loss"):
        left = core["values"].get(metric)
        right = global_["values"].get(metric)
        if left is None or right is None:
            result[metric] = {"delta_global_minus_core": None, "reason": "metric unavailable in one experiment"}
        else:
            result[metric] = {"delta_global_minus_core": float(right - left)}
    return result


def comparison_report(core: Mapping[str, Any], augmented: Mapping[str, Any], metadata: Mapping[str, Any], *, augmented_name: str) -> dict[str, Any]:
    models: dict[str, Any] = {}
    for name in core["models"]:
        core_dates = [row["prediction_date"] for row in core["models"][name]["oos_predictions"]]
        augmented_dates = [row["prediction_date"] for row in augmented["models"][name]["oos_predictions"]]
        if core_dates != augmented_dates:
            raise AssertionError(f"OOS dates differ for {name}")
        years = sorted(set(core["models"][name]["year_metrics"]) | set(augmented["models"][name]["year_metrics"]))
        models[name] = {
            "oos_prediction_dates": core_dates,
            "overall_delta": _metric_deltas(core["models"][name]["overall_metrics"], augmented["models"][name]["overall_metrics"]),
            "year_delta": {
                year: _metric_deltas(core["models"][name]["year_metrics"].get(year, {"values": {}}),
                                     augmented["models"][name]["year_metrics"].get(year, {"values": {}}))
                for year in years
            },
        }
    return {"version": VERSION, "experiment": f"tx_core_vs_{augmented_name}", "augmented_features": metadata,
            "tx_core": core, augmented_name: augmented, "comparison": {"models": models}}


def streak_oos_descriptives(rows: Mapping[str, Mapping[str, Any]], oos_records: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Describe realized OOS labels by streak level; this does not select a threshold."""
    selected = {record["prediction_date"] for record in oos_records}
    output: dict[str, dict[str, dict[str, Any]]] = {column: {} for column in STREAK_COLUMNS}
    for day in selected:
        row = rows[day]
        target = row.get(BINARY_COLUMN)
        for column in STREAK_COLUMNS:
            key = "null" if row.get(column) is None else str(row[column])
            entry = output[column].setdefault(key, {"samples": 0, "observed_targets": 0, "up_count": 0, "up_rate": None})
            entry["samples"] += 1
            if target is not None:
                entry["observed_targets"] += 1
                entry["up_count"] += int(target == UP)
    for groups in output.values():
        for entry in groups.values():
            if entry["observed_targets"]:
                entry["up_rate"] = entry["up_count"] / entry["observed_targets"]
    return {"descriptive_only": True, "by_feature": output}


def positioning_comparison_report(experiments: Mapping[str, Mapping[str, Any]], metadata: Mapping[str, Any]) -> dict[str, Any]:
    """Compare fixed feature sets against Core while enforcing identical OOS dates."""
    core = experiments["tx_core"]
    comparisons: dict[str, Any] = {}
    for experiment, report in experiments.items():
        if experiment == "tx_core":
            continue
        comparisons[experiment] = comparison_report(core, report, metadata, augmented_name=experiment)["comparison"]
    return {
        "version": VERSION,
        "experiment": "tx_core_positioning_comparison",
        "exploratory_notice": "Repeated use of this OOS period is exploratory comparison, not final independent validation.",
        "positioning_features": metadata,
        "experiments": dict(experiments),
        "relative_to_tx_core": comparisons,
    }


def select_coverage_guard_features(train_rows: list[dict[str, Any]], *,
                                   core_columns: tuple[str, ...], additional_columns: tuple[str, ...],
                                   min_non_null: int = 252, min_coverage: float = 0.5) -> dict[str, Any]:
    """Choose optional positioning columns only from this fold's train rows."""
    if not train_rows:
        raise ValueError("Coverage guard requires nonempty training rows")
    if min_non_null < 1 or not 0 < min_coverage <= 1:
        raise ValueError("Invalid fixed coverage guard threshold")
    retained = list(core_columns)
    excluded = []
    for column in additional_columns:
        non_null = sum(row.get(column) is not None for row in train_rows)
        coverage = non_null / len(train_rows)
        reasons = []
        if non_null < min_non_null:
            reasons.append(f"non_null<{min_non_null}")
        if coverage < min_coverage:
            reasons.append(f"coverage<{min_coverage}")
        if reasons:
            excluded.append({"column": column, "non_null": non_null, "coverage": coverage, "reasons": reasons})
        else:
            retained.append(column)
    return {
        "rule": {"min_non_null": min_non_null, "min_coverage": min_coverage},
        "retained_columns": retained, "excluded_columns": excluded,
        "fallback_to_tx_core": len(retained) == len(core_columns),
    }


def run_rows_with_coverage_guard(rows: list[dict[str, Any]], *, additional_columns: tuple[str, ...],
                                 experiment_name: str, initial_train_size: int = 504, test_size: int = 21,
                                 data_identity: dict[str, Any] | None = None,
                                 core_columns: tuple[str, ...] = FEATURE_COLUMNS,
                                 expected_time: time = time(8, 30),
                                 feature_available_field: str = "feature_available",
                                 allow_resolved_flat: bool = False,
                                 task_name: str = "B4 same-day TX regular close/open") -> dict[str, Any]:
    """Fixed-rule positioning benchmark where optional columns vary by train fold."""
    for row in rows:
        _validate_row(row, expected_time=expected_time, allow_resolved_flat=allow_resolved_flat,
                      task_label=task_name, feature_available_field=feature_available_field)
    indexed, excluded = _candidate_rows(rows, (*core_columns, *additional_columns),
                                        feature_available_field=feature_available_field)
    folds = expanding_walk_forward_prepared(indexed, initial_train_size=initial_train_size, test_size=test_size,
                                             allow_resolved_flat=allow_resolved_flat)
    by_index = {index: row for index, row in indexed}
    model_records: dict[str, list[dict[str, Any]]] = defaultdict(list)
    fold_reports: list[dict[str, Any]] = []
    for number, fold in enumerate(folds, start=1):
        train_rows = [by_index[index] for index in fold.train_indices]
        test_rows = [by_index[index] for index in fold.test_indices]
        guard = select_coverage_guard_features(train_rows, core_columns=core_columns, additional_columns=additional_columns)
        active = tuple(guard["retained_columns"])
        fold_report = {
            "fold": number, "fit_at": fold.fit_at.isoformat(), "train_count": len(train_rows),
            "test_count": len(test_rows), "purged_count": len(fold.purged_indices),
            "train_indices": list(fold.train_indices), "test_indices": list(fold.test_indices),
            "purged_indices": list(fold.purged_indices),
            "test_start": test_rows[0]["prediction_date"].isoformat(), "test_end": test_rows[-1]["prediction_date"].isoformat(),
            "coverage_guard": guard,
            "training_feature_diagnostics": _training_feature_diagnostics(train_rows, (*core_columns, *additional_columns)),
            "models": {},
        }
        for name, model in _models().items():
            records, diagnostics = _fold_model_records(name, model, train_rows, test_rows, number, active)
            model_records[name].extend(records)
            fold_report["models"][name] = {"diagnostics": diagnostics, "metrics": _metric_bundle(records)}
        fold_reports.append(fold_report)
    models_report = {}
    for name, records in model_records.items():
        by_year: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for record in records:
            by_year[record["prediction_date"][:4]].append(record)
        models_report[name] = {"oos_predictions": records, "overall_metrics": _metric_bundle(records),
                               "year_metrics": {year: _metric_bundle(items) for year, items in sorted(by_year.items())}}
    return {
        "version": VERSION, "experiment": experiment_name,
        "task": {"name": task_name, "labels": ({"DOWN": DOWN, "UP": UP, "FLAT": "excluded from binary scoring"}
                                                    if allow_resolved_flat else {"DOWN": DOWN, "UP": UP})},
        "dataset": data_identity or {"rows": len(rows)}, "feature_columns": list(core_columns),
        "coverage_guard": {"rule": {"min_non_null": 252, "min_coverage": 0.5}, "additional_columns": list(additional_columns)},
        "row_counts": {"dataset": len(rows), "model_candidates": len(indexed), "excluded_feature_unavailable": len(excluded)},
        "excluded_rows": excluded, "folds": fold_reports, "models": models_report,
    }


def _validate_original_positioning_report(original: Mapping[str, Any], current_core: Mapping[str, Any]) -> None:
    if original.get("experiment") != "tx_core_positioning_comparison":
        raise ValueError("Original report is not a positioning comparison")
    old_core = original.get("experiments", {}).get("tx_core")
    if not isinstance(old_core, Mapping):
        raise ValueError("Original report lacks tx_core experiment")
    if old_core.get("dataset") != current_core.get("dataset"):
        raise ValueError("Original report data identity differs from current B4 data")
    old_folds = [(item.get("fit_at"), item.get("train_indices"), item.get("test_indices"), item.get("purged_indices")) for item in old_core.get("folds", [])]
    new_folds = [(item.get("fit_at"), item.get("train_indices"), item.get("test_indices"), item.get("purged_indices")) for item in current_core.get("folds", [])]
    if old_folds != new_folds:
        raise ValueError("Original report folds differ from current fixed walk-forward folds")
    for model, old_model in old_core.get("models", {}).items():
        new_model = current_core.get("models", {}).get(model)
        if new_model is None:
            raise ValueError(f"Current runner lacks original model {model}")
        old_labels = [(row.get("prediction_date"), row.get("fold"), row.get("target")) for row in old_model.get("oos_predictions", [])]
        new_labels = [(row.get("prediction_date"), row.get("fold"), row.get("target")) for row in new_model.get("oos_predictions", [])]
        if old_labels != new_labels:
            raise ValueError(f"Original report OOS dates/labels differ for {model}")
    core_folds = [(item.get("fit_at"), item.get("train_indices"), item.get("test_indices"), item.get("purged_indices")) for item in old_core.get("folds", [])]
    for name, report in original.get("experiments", {}).items():
        folds = [(item.get("fit_at"), item.get("train_indices"), item.get("test_indices"), item.get("purged_indices")) for item in report.get("folds", [])]
        if folds != core_folds:
            raise ValueError(f"Original report folds differ across experiment {name}")
        for model, model_report in report.get("models", {}).items():
            labels = [(row.get("prediction_date"), row.get("fold"), row.get("target")) for row in model_report.get("oos_predictions", [])]
            core_labels = [(row.get("prediction_date"), row.get("fold"), row.get("target")) for row in old_core["models"][model].get("oos_predictions", [])]
            if labels != core_labels:
                raise ValueError(f"Original report OOS dates/labels differ across experiment {name}/{model}")


def coverage_guard_report(original: Mapping[str, Any], guarded: Mapping[str, Mapping[str, Any]], metadata: Mapping[str, Any]) -> dict[str, Any]:
    output = {}
    for name, guarded_report in guarded.items():
        original_report = original["experiments"].get(name)
        if not isinstance(original_report, Mapping):
            raise ValueError(f"Original report lacks {name}")
        output[name] = comparison_report(original_report, guarded_report, metadata, augmented_name="coverage_guard")["comparison"]
    focus = {}
    for name, report in guarded.items():
        by_fold = {fold["fold"]: fold for fold in report["folds"]}
        original_folds = {fold["fold"]: fold for fold in original["experiments"][name]["folds"]}
        focus[name] = {
            str(number): {
                "original": original_folds.get(number, {}).get("models", {}),
                "guarded": by_fold.get(number, {}).get("models", {}),
                "coverage_guard": by_fold.get(number, {}).get("coverage_guard"),
            }
            for number in (1, 25)
        }
    return {
        "version": VERSION, "experiment": "positioning_original_vs_train_coverage_guard",
        "rule": {"min_non_null": 252, "min_coverage": 0.5, "selection": "train-only", "scope": "Institutional/PCR additions only"},
        "original_vs_guarded": output, "guarded_experiments": dict(guarded), "focus_folds_1_25": focus,
        "feature_metadata": metadata,
        "exploratory_notice": "This is a fixed-rule exploratory comparison, not final independent validation.",
    }


def write_report(report: Mapping[str, Any], path: Path) -> None:
    if path.exists():
        raise FileExistsError(f"Refusing to overwrite existing baseline report: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def run(options: argparse.Namespace) -> dict[str, Any]:
    rows, identity = load_b4_rows(options.dataset)
    core = run_rows(rows, initial_train_size=options.initial_train_size, test_size=options.test_size,
                    data_identity=identity, experiment_name="tx_core")
    if options.experiment == "core":
        report = core
    elif options.experiment in {"core-plus-global", "comparison"}:
        pit_rows, pit_identity = load_daily_pit_rows(options.daily_features)
        with_global, global_metadata = attach_global_returns(rows, pit_rows)
        global_report = run_rows(
            with_global, initial_train_size=options.initial_train_size, test_size=options.test_size,
            data_identity={"b4": identity, "daily_pit": pit_identity},
            feature_columns=(*FEATURE_COLUMNS, *GLOBAL_RETURN_COLUMNS), experiment_name="tx_core_plus_global",
        )
        report = global_report if options.experiment == "core-plus-global" else comparison_report(
            core, global_report, global_metadata, augmented_name="tx_core_plus_global",
        )
    elif options.experiment in {"core-plus-streak", "core-vs-streak"}:
        streak_rows, streak_metadata = attach_streak_features(rows)
        streak_report = run_rows(
            streak_rows, initial_train_size=options.initial_train_size, test_size=options.test_size,
            data_identity=identity, feature_columns=(*FEATURE_COLUMNS, *STREAK_COLUMNS),
            experiment_name="tx_core_plus_streak",
        )
        if options.experiment == "core-plus-streak":
            report = streak_report
        else:
            report = comparison_report(core, streak_report, streak_metadata, augmented_name="tx_core_plus_streak")
            by_day = {row["prediction_date"].isoformat(): row for row in streak_rows}
            report["streak_oos_descriptives"] = streak_oos_descriptives(
                by_day, core["models"]["dummy"]["oos_predictions"],
            )
    elif options.experiment == "positioning-coverage-guard":
        original = json.loads(options.original_report.read_text(encoding="utf-8"))
        _validate_original_positioning_report(original, core)
        pit_rows, pit_identity = load_daily_pit_rows(options.daily_features)
        positioned_rows, positioning_metadata = attach_positioning_features(rows, pit_rows)
        identity_with_pit = {"b4": identity, "daily_pit": pit_identity}
        for name in ("tx_core_plus_institutional", "tx_core_plus_pcr", "tx_core_plus_institutional_pcr"):
            if original["experiments"].get(name, {}).get("dataset") != identity_with_pit:
                raise ValueError(f"Original report data identity differs for {name}")
        guarded = {
            "tx_core": core,
            "tx_core_plus_institutional": run_rows_with_coverage_guard(
                positioned_rows, additional_columns=INSTITUTIONAL_COLUMNS, experiment_name="tx_core_plus_institutional_coverage_guard",
                initial_train_size=options.initial_train_size, test_size=options.test_size, data_identity=identity_with_pit,
            ),
            "tx_core_plus_pcr": run_rows_with_coverage_guard(
                positioned_rows, additional_columns=PCR_COLUMNS, experiment_name="tx_core_plus_pcr_coverage_guard",
                initial_train_size=options.initial_train_size, test_size=options.test_size, data_identity=identity_with_pit,
            ),
            "tx_core_plus_institutional_pcr": run_rows_with_coverage_guard(
                positioned_rows, additional_columns=POSITIONING_COLUMNS, experiment_name="tx_core_plus_institutional_pcr_coverage_guard",
                initial_train_size=options.initial_train_size, test_size=options.test_size, data_identity=identity_with_pit,
            ),
        }
        report = coverage_guard_report(original, guarded, positioning_metadata)
    else:
        pit_rows, pit_identity = load_daily_pit_rows(options.daily_features)
        positioned_rows, positioning_metadata = attach_positioning_features(rows, pit_rows)
        identity_with_pit = {"b4": identity, "daily_pit": pit_identity}
        experiments = {
            "tx_core": core,
            "tx_core_plus_institutional": run_rows(
                positioned_rows, initial_train_size=options.initial_train_size, test_size=options.test_size,
                data_identity=identity_with_pit, feature_columns=(*FEATURE_COLUMNS, *INSTITUTIONAL_COLUMNS),
                experiment_name="tx_core_plus_institutional",
            ),
            "tx_core_plus_pcr": run_rows(
                positioned_rows, initial_train_size=options.initial_train_size, test_size=options.test_size,
                data_identity=identity_with_pit, feature_columns=(*FEATURE_COLUMNS, *PCR_COLUMNS),
                experiment_name="tx_core_plus_pcr",
            ),
            "tx_core_plus_institutional_pcr": run_rows(
                positioned_rows, initial_train_size=options.initial_train_size, test_size=options.test_size,
                data_identity=identity_with_pit, feature_columns=(*FEATURE_COLUMNS, *INSTITUTIONAL_COLUMNS, *PCR_COLUMNS),
                experiment_name="tx_core_plus_institutional_pcr",
            ),
        }
        report = positioning_comparison_report(experiments, positioning_metadata)
    write_report(report, options.output)
    folds = len(core["folds"])
    return {"output": str(options.output), "experiment": options.experiment, "folds": folds,
            "model_candidates": core["row_counts"]["model_candidates"]}


if __name__ == "__main__":
    print(json.dumps(run(arguments()), ensure_ascii=False, indent=2))
