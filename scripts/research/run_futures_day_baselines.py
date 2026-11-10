"""Run fixed Dummy/Logistic/HGB baselines for the independent V5 DAY dataset.

This is an exploratory walk-forward benchmark, not a live signal generator.
It reuses the B4 model factory and train-only preprocessing while retaining
DAY's 08:15 cutoff and resolved-FLAT semantics.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from datetime import time
from pathlib import Path
from typing import Any

from config import PROJECT_ROOT
from research import build_futures_day_dataset as day
from research import run_futures_b4_baselines as baseline

VERSION = "v5.day-baselines-1.1"
DEFAULT_OUTPUT = PROJECT_ROOT / "reports" / "futures_day_baselines_v5_1.json"
DEFAULT_COMPARISON_OUTPUT = PROJECT_ROOT / "reports" / "futures_day_core_vs_global_v5_1.json"
DEFAULT_POSITIONING_OUTPUT = PROJECT_ROOT / "reports" / "futures_day_positioning_comparison_v5_1.json"


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=day.OUTPUT_ROOT)
    parser.add_argument("--daily-features", type=Path, default=baseline.DAILY_FEATURE_ROOT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--experiment",
        choices=("core", "core-plus-global", "core-vs-global", "positioning-comparison"),
        default="core",
        help="DAY TX Core baseline or the fixed TX Core versus Global comparison.",
    )
    parser.add_argument(
        "--repair-report",
        type=Path,
        help="Read an existing DAY report and recompute derived relative-to-dummy statistics only.",
    )
    parser.add_argument("--initial-train-size", type=int, default=504)
    parser.add_argument("--test-size", type=int, default=21)
    return parser.parse_args()


def load_day_rows(root: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    import pyarrow.parquet as pq
    paths = sorted(root.rglob("*.parquet"))
    if not paths:
        raise FileNotFoundError(f"No DAY Parquet files: {root}")
    schema = None
    rows: list[dict[str, Any]] = []
    files = []
    for path in paths:
        table = pq.ParquetFile(path).read()
        if schema is not None and not table.schema.equals(schema, check_metadata=False):
            raise ValueError(f"DAY Parquet schema mismatch: {path}")
        schema = table.schema
        rows.extend(table.to_pylist())
        files.append({"path": str(path.relative_to(root)).replace("\\", "/"), "bytes": path.stat().st_size,
                      "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
    dates = [row.get("prediction_date") for row in rows]
    if len(dates) != len(set(dates)):
        raise ValueError("Duplicate DAY prediction_date")
    return sorted(rows, key=lambda row: row["prediction_at"]), {
        "dataset_version": day.VERSION, "rows": len(rows), "schema": str(schema), "files": files,
    }


def _relative_to_dummy(report: dict[str, Any]) -> dict[str, Any]:
    models = report["models"]
    output: dict[str, Any] = {}
    def is_scorable(record: dict[str, Any]) -> bool:
        """Match the shared metric bundle's observed-record eligibility exactly."""
        return record.get("target") is not None and record.get("prediction") is not None

    dummy_by_date = {
        row["prediction_date"]: row
        for row in models["dummy"]["oos_predictions"]
        if is_scorable(row)
    }
    for name in ("logistic", "hist_gradient_boosting"):
        current = {
            row["prediction_date"]: row
            for row in models[name]["oos_predictions"]
            if is_scorable(row)
        }
        common = sorted(set(dummy_by_date) & set(current))
        dummy_records = [dummy_by_date[item] for item in common]
        model_records = [current[item] for item in common]
        dummy_metrics = baseline._metric_bundle(dummy_records)
        model_metrics = baseline._metric_bundle(model_records)
        difference = {}
        for metric in ("accuracy", "balanced_accuracy", "roc_auc", "brier", "log_loss"):
            left = model_metrics["values"].get(metric)
            right = dummy_metrics["values"].get(metric)
            difference[metric] = None if left is None or right is None else left - right
        output[name] = {"common_scorable_dates": common, "common_scorable_count": len(common),
                        "model_metrics": model_metrics, "dummy_metrics": dummy_metrics, "difference": difference}
    return output


def _assert_output_available(path: Path) -> None:
    """Fail before loading data or fitting any models when an output already exists."""
    if path.exists():
        raise FileExistsError(f"Refusing to overwrite existing baseline report: {path}")


def repair_report(source: Path) -> dict[str, Any]:
    """Recompute only derived comparison statistics from persisted OOS predictions."""
    report = json.loads(source.read_text(encoding="utf-8"))
    if report.get("experiment") != "day_tx_core":
        raise ValueError("Repair source is not a DAY baseline report")
    models = report.get("models")
    if not isinstance(models, dict) or not {"dummy", "logistic", "hist_gradient_boosting"} <= set(models):
        raise ValueError("Repair source lacks required DAY model OOS predictions")
    report["relative_to_dummy"] = _relative_to_dummy(report)
    report["derived_statistics_repaired"] = {
        "source_report": str(source),
        "scope": "relative_to_dummy common scorable dates and metric deltas only",
        "training_or_predictions_recomputed": False,
    }
    return report


def run_rows(rows: list[dict[str, Any]], *, initial_train_size: int = 504, test_size: int = 21,
             data_identity: dict[str, Any] | None = None,
             feature_columns: tuple[str, ...] = day.FEATURE_COLUMNS,
             experiment_name: str = "day_tx_core") -> dict[str, Any]:
    report = baseline.run_rows(
        rows, initial_train_size=initial_train_size, test_size=test_size, data_identity=data_identity,
        feature_columns=feature_columns, experiment_name=experiment_name,
        expected_time=time(8, 15), feature_available_field="feature_pit_passed",
        allow_resolved_flat=True, task_name="DAY 08:15 TX regular open-to-close",
    )
    # Keep the shared runner generic: derive its boolean candidate field here
    # rather than changing the persisted DAY schema.
    return report


def prepare_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Add an in-memory candidate flag; never modify the source dataset."""
    prepared = []
    for source in rows:
        row = dict(source)
        row["feature_pit_passed"] = row.get("feature_pit_status") == "passed_0815"
        prepared.append(row)
    return prepared


def build_report(rows: list[dict[str, Any]], *, initial_train_size: int, test_size: int,
                 data_identity: dict[str, Any] | None = None) -> dict[str, Any]:
    core = run_rows(prepare_rows(rows), initial_train_size=initial_train_size, test_size=test_size,
                    data_identity=data_identity)
    core["version"] = VERSION
    core["exploratory_notice"] = (
        "Exploratory walk-forward backtest only. It does not establish live tradability, "
        "historical contract-rule vintage, or predictive advantage."
    )
    core["dataset_limitations"] = {
        "contract_selection_evidence": dict(sorted(Counter(row["contract_selection_evidence_status"] for row in rows).items())),
        "live_readiness": dict(sorted(Counter(row.get("live_readiness", "unknown") for row in rows).items())),
        "flat_policy": "Resolved FLAT labels remain in OOS dates but are excluded from binary training and scoring.",
        "missing_label_policy": "Missing labels remain in OOS dates but are excluded from binary training and scoring.",
    }
    core["relative_to_dummy"] = _relative_to_dummy(core)
    return core


def _is_scorable(record: dict[str, Any]) -> bool:
    """Use the same observed-record condition as the shared metric bundle."""
    return record.get("target") is not None and record.get("prediction") is not None


def _common_scoring(core: dict[str, Any], augmented: dict[str, Any], *, augmented_name: str) -> dict[str, Any]:
    """Recalculate comparison metrics on a single shared scored-date denominator."""
    output: dict[str, Any] = {}
    for name in ("dummy", "logistic", "hist_gradient_boosting"):
        core_by_date = {
            item["prediction_date"]: item
            for item in core["models"][name]["oos_predictions"] if _is_scorable(item)
        }
        augmented_by_date = {
            item["prediction_date"]: item
            for item in augmented["models"][name]["oos_predictions"] if _is_scorable(item)
        }
        common = sorted(set(core_by_date) & set(augmented_by_date))
        core_records = [core_by_date[item] for item in common]
        augmented_records = [augmented_by_date[item] for item in common]
        if any(core_by_date[item]["target"] != augmented_by_date[item]["target"] for item in common):
            raise AssertionError(f"DAY target differs between Core and {augmented_name} for {name}")
        core_metrics = baseline._metric_bundle(core_records)
        augmented_metrics = baseline._metric_bundle(augmented_records)
        by_year: dict[str, dict[str, list[dict[str, Any]]]] = {}
        for prediction_date in common:
            bucket = by_year.setdefault(prediction_date[:4], {"core": [], "augmented": []})
            bucket["core"].append(core_by_date[prediction_date])
            bucket["augmented"].append(augmented_by_date[prediction_date])
        yearly = {}
        for year, records in sorted(by_year.items()):
            year_core = baseline._metric_bundle(records["core"])
            year_augmented = baseline._metric_bundle(records["augmented"])
            yearly[year] = {
                "tx_core_metrics": year_core,
                f"{augmented_name}_metrics": year_augmented,
                "difference": baseline._metric_deltas(year_core, year_augmented),
            }
        output[name] = {
            "common_scorable_dates": common,
            "common_scorable_count": len(common),
            "tx_core_metrics": core_metrics,
            f"{augmented_name}_metrics": augmented_metrics,
            "difference": baseline._metric_deltas(core_metrics, augmented_metrics),
            "yearly": yearly,
        }
    return output


def build_global_comparison_report(
    rows: list[dict[str, Any]], *, pit_rows: dict[Any, dict[str, Any]],
    pit_identity: dict[str, Any], initial_train_size: int, test_size: int,
    data_identity: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run fixed DAY Core/Global experiments using B4's PIT-safe return derivation."""
    prepared = prepare_rows(rows)
    core = run_rows(prepared, initial_train_size=initial_train_size, test_size=test_size,
                    data_identity=data_identity)
    global_rows, global_metadata = baseline.attach_global_returns(prepared, pit_rows)
    global_report = run_rows(
        global_rows, initial_train_size=initial_train_size, test_size=test_size,
        data_identity={"day": data_identity, "daily_pit": pit_identity},
        feature_columns=(*day.FEATURE_COLUMNS, *baseline.GLOBAL_RETURN_COLUMNS),
        experiment_name="day_tx_core_plus_global",
    )
    comparison = baseline.comparison_report(
        core, global_report, global_metadata, augmented_name="tx_core_plus_global",
    )
    common_scoring = _common_scoring(core, global_report, augmented_name="tx_core_plus_global")
    # The generic B4 comparison preserves whole-report metrics.  DAY's
    # deltas are deliberately replaced by the shared scored-date denominator.
    for name, shared in common_scoring.items():
        comparison["comparison"]["models"][name].update({
            "common_scorable_dates": shared["common_scorable_dates"],
            "common_scorable_count": shared["common_scorable_count"],
            "overall_delta": shared["difference"],
            "year_delta": {year: values["difference"] for year, values in shared["yearly"].items()},
        })
    comparison.update({
        "version": VERSION,
        "experiment": "day_tx_core_vs_tx_core_plus_global",
        "exploratory_notice": (
            "Exploratory walk-forward comparison only. Global fields are daily source-observation returns, "
            "not NQ/ES intraday data or manually weighted probability adjustments."
        ),
        "global_feature_policy": {
            "included": list(baseline.GLOBAL_RETURN_COLUMNS),
            "dgs10": "excluded/quarantined",
            "missing_values": "retained; train-only preprocessing handles optional Global feature gaps",
            "same_oos_dates": "required for each model before comparison",
        },
        "common_scoring": common_scoring,
    })
    return comparison


def _guarded_day_positioning_report(
    rows: list[dict[str, Any]], *, additional_columns: tuple[str, ...], experiment_name: str,
    initial_train_size: int, test_size: int, data_identity: dict[str, Any],
) -> dict[str, Any]:
    report = baseline.run_rows_with_coverage_guard(
        rows,
        additional_columns=additional_columns,
        experiment_name=experiment_name,
        initial_train_size=initial_train_size,
        test_size=test_size,
        data_identity=data_identity,
        core_columns=day.FEATURE_COLUMNS,
        expected_time=time(8, 15),
        feature_available_field="feature_pit_passed",
        allow_resolved_flat=True,
        task_name="DAY 08:15 TX regular open-to-close",
    )
    report["version"] = VERSION
    report["relative_to_dummy"] = _relative_to_dummy(report)
    return report


def build_positioning_comparison_report(
    rows: list[dict[str, Any]], *, pit_rows: dict[Any, dict[str, Any]],
    pit_identity: dict[str, Any], initial_train_size: int, test_size: int,
    data_identity: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Four fixed DAY Core/positioning experiments with train-only coverage guard."""
    prepared = prepare_rows(rows)
    core = build_report(rows, initial_train_size=initial_train_size, test_size=test_size,
                        data_identity=data_identity)
    positioned_rows, positioning_metadata = baseline.attach_positioning_features(prepared, pit_rows)
    identity_with_pit = {"day": data_identity, "daily_pit": pit_identity}
    definitions = {
        "tx_core_plus_institutional": baseline.INSTITUTIONAL_COLUMNS,
        "tx_core_plus_pcr": baseline.PCR_COLUMNS,
        "tx_core_plus_institutional_pcr": baseline.POSITIONING_COLUMNS,
    }
    experiments: dict[str, dict[str, Any]] = {"tx_core": core}
    for name, columns in definitions.items():
        experiments[name] = _guarded_day_positioning_report(
            positioned_rows, additional_columns=columns, experiment_name=f"day_{name}",
            initial_train_size=initial_train_size, test_size=test_size, data_identity=identity_with_pit,
        )
    relative_to_core: dict[str, Any] = {}
    common_scoring: dict[str, Any] = {}
    for name, report in experiments.items():
        if name == "tx_core":
            continue
        comparison = baseline.comparison_report(core, report, positioning_metadata, augmented_name=name)
        shared = _common_scoring(core, report, augmented_name=name)
        for model, values in shared.items():
            comparison["comparison"]["models"][model].update({
                "common_scorable_dates": values["common_scorable_dates"],
                "common_scorable_count": values["common_scorable_count"],
                "overall_delta": values["difference"],
                "year_delta": {year: details["difference"] for year, details in values["yearly"].items()},
            })
        relative_to_core[name] = comparison["comparison"]
        common_scoring[name] = shared
    return {
        "version": VERSION,
        "experiment": "day_tx_core_positioning_comparison",
        "exploratory_notice": (
            "Exploratory walk-forward comparison only. Optional positioning fields are selected solely from each "
            "training fold; this does not establish predictive advantage or justify model selection."
        ),
        "positioning_features": positioning_metadata,
        "coverage_guard": {
            "rule": {"min_non_null": 252, "min_coverage": 0.5, "selection": "train-only"},
            "scope": "Institutional/PCR additions only; the 13 DAY TX Core columns never change.",
            "fallback": "If all optional columns fail, the fold uses TX Core only and adds no optional missing indicators.",
        },
        "experiments": experiments,
        "relative_to_tx_core": relative_to_core,
        "common_scoring": common_scoring,
        "relative_to_dummy": {name: report["relative_to_dummy"] for name, report in experiments.items()},
    }


def run(options: argparse.Namespace) -> dict[str, Any]:
    _assert_output_available(options.output)
    if options.repair_report:
        report = repair_report(options.repair_report)
        baseline.write_report(report, options.output)
        return report
    rows, identity = load_day_rows(options.dataset)
    if options.experiment == "core":
        report = build_report(rows, initial_train_size=options.initial_train_size, test_size=options.test_size,
                              data_identity=identity)
    elif options.experiment in {"core-plus-global", "core-vs-global"}:
        pit_rows, pit_identity = baseline.load_daily_pit_rows(options.daily_features)
        report = build_global_comparison_report(
            rows, pit_rows=pit_rows, pit_identity=pit_identity,
            initial_train_size=options.initial_train_size, test_size=options.test_size,
            data_identity=identity,
        )
        if options.experiment == "core-plus-global":
            report = report["tx_core_plus_global"]
    else:
        pit_rows, pit_identity = baseline.load_daily_pit_rows(options.daily_features)
        report = build_positioning_comparison_report(
            rows, pit_rows=pit_rows, pit_identity=pit_identity,
            initial_train_size=options.initial_train_size, test_size=options.test_size,
            data_identity=identity,
        )
    baseline.write_report(report, options.output)
    return report


if __name__ == "__main__":
    print(json.dumps(run(arguments()), ensure_ascii=False, indent=2, default=str))
