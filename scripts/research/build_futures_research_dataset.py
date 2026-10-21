"""Join PIT-safe futures features to their already-defined next-session targets.

This is intentionally a left join from features: a prediction row is retained even
when its next-session target is not yet observable.
"""

from __future__ import annotations

import argparse
import json
import os
from collections import Counter
from datetime import date
from pathlib import Path
from typing import Any

from config import PROJECT_ROOT
from research import build_futures_daily_pit_alignment as feature_builder
from research import build_futures_daily_targets as target_builder


VERSION = "v4.6-1.0"
DEFAULT_START = date(2021, 10, 14)
FEATURE_ROOT = feature_builder.OUTPUT_ROOT
TARGET_ROOT = target_builder.OUTPUT_ROOT
OUTPUT_ROOT = PROJECT_ROOT / "data_lake" / "futures_prediction" / "research_dataset"

TARGET_COLUMNS = (
    "target_version",
    "target_trading_date",
    "target_contract",
    "target_available_at",
    "next_trading_day_regular_return",
    "direction_class",
    "direction_down_max",
    "direction_up_min",
)


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build the futures daily research dataset")
    parser.add_argument("--start", type=date.fromisoformat, default=DEFAULT_START)
    parser.add_argument(
        "--end", type=lambda value: date.today() if value == "today" else date.fromisoformat(value),
        default=date.today(),
    )
    parser.add_argument("--features", type=Path, default=FEATURE_ROOT)
    parser.add_argument("--targets", type=Path, default=TARGET_ROOT)
    parser.add_argument("--output", type=Path, default=OUTPUT_ROOT)
    options = parser.parse_args()
    if options.start > options.end:
        parser.error("--start cannot exceed --end")
    return options


def read_rows(root: Path, start: date, end: date) -> list[dict[str, Any]]:
    import pyarrow.parquet as pq

    rows: list[dict[str, Any]] = []
    for path in sorted(root.rglob("*.parquet")):
        for row in pq.ParquetFile(path).read().to_pylist():
            prediction_date = row["prediction_date"]
            if start <= prediction_date <= end:
                rows.append(row)
    return rows


def _rows_by_prediction_date(rows: list[dict[str, Any]], label: str) -> dict[date, dict[str, Any]]:
    output: dict[date, dict[str, Any]] = {}
    for row in rows:
        prediction_date = row["prediction_date"]
        if prediction_date in output:
            raise ValueError(f"Duplicate {label} prediction_date: {prediction_date}")
        output[prediction_date] = row
    return output


def build_dataset(features: list[dict[str, Any]], targets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Left-join targets onto immutable feature rows by prediction date."""
    feature_by_date = _rows_by_prediction_date(features, "feature")
    target_by_date = _rows_by_prediction_date(targets, "target")
    output: list[dict[str, Any]] = []

    for prediction_date in sorted(feature_by_date):
        # Copy first, so target values cannot replace or derive feature values.
        row = dict(feature_by_date[prediction_date])
        target = target_by_date.get(prediction_date)
        if target is not None and target.get("prediction_at") != row.get("prediction_at"):
            raise ValueError(f"Target prediction_at does not match feature: {prediction_date}")
        for column in TARGET_COLUMNS:
            row[column] = target.get(column) if target is not None else None
        row["target_quality_flags"] = target.get("quality_flags") if target is not None else None
        row["research_dataset_version"] = VERSION
        output.append(row)

    if len(output) != len(feature_by_date):
        raise ValueError("Research dataset unexpectedly changed feature row count")
    return output


def output_schema() -> Any:
    import pyarrow as pa

    target_schema = target_builder.output_schema()
    fields = list(feature_builder.output_schema())
    fields.extend(target_schema.field(column) for column in TARGET_COLUMNS)
    fields.extend([
        pa.field("target_quality_flags", pa.string()),
        pa.field("research_dataset_version", pa.string()),
    ])
    return pa.schema(fields)


def parquet_rows(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    import pyarrow.parquet as pq
    return pq.ParquetFile(path).read().to_pylist()


def write_rows(rows: list[dict[str, Any]], root: Path) -> dict[str, int]:
    import pyarrow as pa
    import pyarrow.parquet as pq

    groups: dict[int, list[dict[str, Any]]] = {}
    for row in rows:
        groups.setdefault(row["prediction_date"].year, []).append(row)
    for year, additions in sorted(groups.items()):
        path = root / f"partition_year={year}" / "part-00000.parquet"
        merged = {row["prediction_date"]: row for row in parquet_rows(path)}
        merged.update({row["prediction_date"]: row for row in additions})
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        try:
            table = pa.Table.from_pylist([merged[key] for key in sorted(merged)], schema=output_schema())
            pq.write_table(table, temporary, compression="zstd", compression_level=6)
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
    return {"rows": len(rows), "files": len(groups)}


def dataset_stats(rows: list[dict[str, Any]]) -> dict[str, Any]:
    classes = Counter(row["direction_class"] for row in rows if row["direction_class"] is not None)
    dates = [row["prediction_date"] for row in rows]
    return {
        "target_available": sum(row["next_trading_day_regular_return"] is not None for row in rows),
        "target_missing": sum(row["next_trading_day_regular_return"] is None for row in rows),
        "class_distribution": dict(sorted(classes.items())),
        "first_prediction_date": min(dates).isoformat() if dates else None,
        "last_prediction_date": max(dates).isoformat() if dates else None,
    }


def run(options: argparse.Namespace) -> dict[str, Any]:
    features = read_rows(options.features, options.start, options.end)
    targets = read_rows(options.targets, options.start, options.end)
    rows = build_dataset(features, targets)
    return {
        "version": VERSION,
        "start": options.start.isoformat(),
        "end": options.end.isoformat(),
        **write_rows(rows, options.output),
        **dataset_stats(rows),
    }


def main() -> int:
    print(json.dumps(run(arguments()), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
