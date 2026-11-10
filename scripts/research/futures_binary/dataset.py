"""Read-only dataset boundary. This module never constructs a model matrix.

Timestamp validation is necessary but does not certify historical data vintages
or the provenance of every derived feature. Feature selection belongs to a
separate explicit allowlist; callers must never pass all numeric columns to ML.
"""

from __future__ import annotations

import math
from datetime import date, datetime, time
from numbers import Real
from pathlib import Path
from typing import Any, Iterable, Mapping
from zoneinfo import ZoneInfo


TAIPEI = ZoneInfo("Asia/Taipei")
RETURN_COLUMN = "next_trading_day_regular_return"
BINARY_COLUMN = "binary_direction_target"
REQUIRED_COLUMNS = frozenset((
    "prediction_date", "prediction_at", "tx_available_at",
    "max_feature_available_at", "target_trading_date", "target_available_at",
    RETURN_COLUMN,
))


def binary_target(value: Any) -> int | None:
    """UP=1, DOWN=0 (including zero); missing is never DOWN."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, Real) or not math.isfinite(value):
        raise ValueError("Target return must be a finite number or None")
    return int(value > 0)


def _aware(value: Any, name: str) -> datetime:
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ValueError(f"{name} must be a timezone-aware datetime")
    return value


def prepare_rows(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Validate, copy, derive binary labels and sort; retain missing labels."""
    result: list[dict[str, Any]] = []
    seen: set[date] = set()
    for source in rows:
        missing = REQUIRED_COLUMNS - source.keys()
        if missing:
            raise ValueError(f"Missing required columns: {sorted(missing)}")
        if BINARY_COLUMN in source:
            raise ValueError(f"Input must not already contain {BINARY_COLUMN}")
        row = dict(source)
        day = row["prediction_date"]
        if type(day) is not date:
            raise ValueError("prediction_date must be a date")
        if day in seen:
            raise ValueError(f"Duplicate prediction_date: {day}")
        seen.add(day)
        cutoff = _aware(row["prediction_at"], "prediction_at")
        local = cutoff.astimezone(TAIPEI)
        if local.date() != day or local.time() != time(8, 30):
            raise ValueError(f"prediction_at must be {day} 08:30 Asia/Taipei")
        for key in ("tx_available_at", "max_feature_available_at"):
            _aware(row[key], key)
        feature_times = []
        for key, value in row.items():
            if key.endswith("_available_at") and key != "target_available_at" and value is not None:
                timestamp = _aware(value, key)
                if timestamp > cutoff:
                    raise ValueError(f"Feature exceeds prediction_at: {day} {key}")
                feature_times.append(timestamp)
            if isinstance(value, Real) and not math.isfinite(value):
                raise ValueError(f"Nonfinite value: {day} {key}")
        if max(feature_times) > row["max_feature_available_at"]:
            raise ValueError(f"max_feature_available_at understates observed availability: {day}")
        label = binary_target(row[RETURN_COLUMN])
        available = row["target_available_at"]
        target_day = row["target_trading_date"]
        if target_day is not None and (type(target_day) is not date or target_day <= day):
            raise ValueError(f"target_trading_date must follow prediction_date: {day}")
        if label is not None and (available is None or target_day is None):
            raise ValueError(f"Observed target requires date and availability: {day}")
        if available is not None:
            available = _aware(available, "target_available_at")
            if available <= cutoff or target_day is None or available.astimezone(TAIPEI).date() < target_day:
                raise ValueError(f"Invalid target availability: {day}")
        row[BINARY_COLUMN] = label
        result.append(row)
    return sorted(result, key=lambda row: row["prediction_at"])


def load_dataset(root: str | Path) -> list[dict[str, Any]]:
    """Read physical Parquet files without synthesizing Hive partition columns.

Require identical physical schemas across partitions. Required metadata types
are checked even for empty files; additional feature columns stay untouched.
"""
    import pyarrow as pa
    import pyarrow.parquet as pq

    paths = sorted(Path(root).rglob("*.parquet"))
    if not paths:
        raise FileNotFoundError(f"No research Parquet files: {root}")
    schema = None
    rows = []
    for path in paths:
        table = pq.ParquetFile(path).read()
        current = table.schema
        if len(set(current.names)) != len(current.names):
            raise ValueError(f"Duplicate column names: {path}")
        if schema is not None and not current.equals(schema, check_metadata=False):
            raise ValueError(f"Parquet schema mismatch: {path}")
        schema = current
        missing = REQUIRED_COLUMNS - set(current.names)
        if missing:
            raise ValueError(f"Missing required columns: {sorted(missing)}")
        for name in REQUIRED_COLUMNS:
            kind = current.field(name).type
            if name.endswith("_at"):
                valid = pa.types.is_timestamp(kind) and kind.tz is not None
            elif name.endswith("_date"):
                valid = pa.types.is_date(kind)
            else:
                valid = pa.types.is_floating(kind) or pa.types.is_integer(kind)
            if not valid:
                raise ValueError(f"Invalid schema type for {name}: {kind}")
        rows.extend(table.to_pylist())
    if not rows:
        raise ValueError("Research dataset contains no rows")
    return prepare_rows(rows)


def training_rows_as_of(rows: Iterable[Mapping[str, Any]], fit_at: datetime) -> list[dict[str, Any]]:
    """Only historical rows whose labels were observable at fold fit time.

Revalidate raw rows rather than trusting a caller-provided derived label.
This is an eligibility boundary, not a walk-forward fold generator.
"""
    _aware(fit_at, "fit_at")
    raw = []
    for source in rows:
        row = dict(source)
        row.pop(BINARY_COLUMN, None)
        raw.append(row)
    return [row for row in prepare_rows(raw)
            if row["prediction_at"] < fit_at
            and row[BINARY_COLUMN] is not None
            and row["target_available_at"] <= fit_at]
