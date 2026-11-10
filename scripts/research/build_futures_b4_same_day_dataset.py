"""Build an independent B4 same-day TX regular-session research dataset.

This builder neither changes V4.6 targets nor uses settlement prices.  It
retains every daily label and marks rows without an existing PIT feature row as
non-candidates rather than deleting their target.
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
from collections import Counter
from datetime import date, datetime, time
from functools import lru_cache
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from config import PROJECT_ROOT
from futures_intraday_reconstruction import contract_expiry
from research import build_futures_daily_pit_alignment as pit
from research.futures_binary import features

VERSION = "v5.b4-1.0"
TAIPEI = ZoneInfo("Asia/Taipei")
TX_ROOT = PROJECT_ROOT / "data_lake" / "staging" / "futures_daily" / "product=TX"
FEATURE_ROOT = PROJECT_ROOT / "data_lake" / "futures_prediction" / "daily_features"
OUTPUT_ROOT = PROJECT_ROOT / "data_lake" / "futures_prediction" / "b4_same_day_dataset"
FEATURE_COLUMNS = features.feature_names()
TARGET_COLUMN = "same_day_regular_return"
BINARY_COLUMN = "binary_direction_target"
AVAILABLE_AT_BASIS = "existing TX daily source convention: trade_date 15:30 Asia/Taipei; not an observed historical publication timestamp"


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tx-root", type=Path, default=TX_ROOT)
    parser.add_argument("--features", type=Path, default=FEATURE_ROOT)
    parser.add_argument("--output", type=Path, default=OUTPUT_ROOT)
    return parser.parse_args()


def _aware(value: Any, field: str) -> datetime:
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return parsed


@lru_cache(maxsize=None)
def _expiry(contract: str) -> date:
    return contract_expiry(contract)


def read_tx_daily(root: Path = TX_ROOT) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in sorted(root.glob("year=*/daily.jsonl.gz")):
        with gzip.open(path, "rt", encoding="utf-8") as source:
            for line in source:
                row = json.loads(line)
                if row.get("product") == "TX" and row.get("session") == "regular":
                    row["trade_date"] = date.fromisoformat(row["trade_date"])
                    row["available_at"] = _aware(row["available_at"], "available_at")
                    rows.append(row)
    return rows


def read_features(root: Path = FEATURE_ROOT) -> dict[date, dict[str, Any]]:
    import pyarrow.parquet as pq

    output: dict[date, dict[str, Any]] = {}
    for path in sorted(root.rglob("*.parquet")):
        for row in pq.ParquetFile(path).read().to_pylist():
            day = row["prediction_date"]
            if day in output:
                raise ValueError(f"Duplicate PIT feature prediction_date: {day}")
            prediction_at = _aware(row["prediction_at"], "prediction_at")
            if prediction_at.astimezone(TAIPEI).time() != time(8, 30):
                raise ValueError(f"PIT feature is not an 08:30 prediction: {day}")
            maximum = row.get("max_feature_available_at")
            if maximum is not None and _aware(maximum, "max_feature_available_at") > prediction_at:
                raise ValueError(f"PIT feature leakage at {day}")
            missing = [column for column in FEATURE_COLUMNS if column not in row]
            if missing:
                raise ValueError(f"PIT feature schema lacks allowlisted columns: {missing}")
            output[day] = row
    return output


def select_contract(rows: list[dict[str, Any]], day: date) -> dict[str, Any] | None:
    """Nearest non-expired listed contract; no volume, OI, close or rank input."""
    candidates: list[tuple[date, str, dict[str, Any]]] = []
    for row in rows:
        try:
            expiry = _expiry(row["contract"])
        except (TypeError, ValueError):
            continue
        if expiry >= day:
            candidates.append((expiry, row["contract"], row))
    return min(candidates, key=lambda item: (item[0], item[1]))[2] if candidates else None


def build_rows(tx_rows: list[dict[str, Any]], pit_features: dict[date, dict[str, Any]]) -> list[dict[str, Any]]:
    by_date: dict[date, list[dict[str, Any]]] = {}
    for row in tx_rows:
        by_date.setdefault(row["trade_date"], []).append(row)
    output: list[dict[str, Any]] = []
    for day, daily_rows in sorted(by_date.items()):
        selected = select_contract(daily_rows, day)
        if selected is None:
            raise ValueError(f"No deterministic non-expired TX contract for {day}")
        prediction_at = pit.prediction_timestamp(day)
        available_at = _aware(selected["available_at"], "target_available_at")
        if available_at <= prediction_at:
            raise ValueError(f"B4 target availability is not after prediction: {day}")
        try:
            open_price, close_price = float(selected["open"]), float(selected["close"])
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError(f"Missing B4 transaction open/close for {day}") from error
        if not open_price:
            raise ValueError(f"Zero B4 transaction open for {day}")
        target = close_price / open_price - 1.0
        feature = pit_features.get(day)
        row: dict[str, Any] = {
            "partition_year": day.year, "prediction_date": day, "prediction_at": prediction_at,
            "dataset_version": VERSION, "feature_available": feature is not None,
            "feature_missing_reason": None if feature is not None else "missing_pit_feature_row",
            "max_feature_available_at": feature.get("max_feature_available_at") if feature else None,
            "target_contract": selected["contract"], "target_source_trading_date": day,
            "target_open": open_price, "target_close": close_price,
            "target_settlement_price": selected.get("settlement_price"),
            "target_available_at": available_at, "target_available_at_basis": AVAILABLE_AT_BASIS,
            TARGET_COLUMN: target, BINARY_COLUMN: int(target > 0),
            "direction_class": "up" if target > 0 else "down",
        }
        for column in FEATURE_COLUMNS:
            row[column] = feature.get(column) if feature else None
        output.append(row)
    return output


def output_schema() -> Any:
    import pyarrow as pa
    pit_schema = pit.output_schema()
    return pa.schema([
        ("partition_year", pa.int64()), ("prediction_date", pa.date32()),
        ("prediction_at", pa.timestamp("us", tz="Asia/Taipei")), ("dataset_version", pa.string()),
        ("feature_available", pa.bool_()), ("feature_missing_reason", pa.string()),
        ("max_feature_available_at", pa.timestamp("us", tz="UTC")), ("target_contract", pa.string()),
        ("target_source_trading_date", pa.date32()), ("target_open", pa.float64()),
        ("target_close", pa.float64()), ("target_settlement_price", pa.float64()),
        ("target_available_at", pa.timestamp("us", tz="UTC")), ("target_available_at_basis", pa.string()),
        (TARGET_COLUMN, pa.float64()), (BINARY_COLUMN, pa.int8()), ("direction_class", pa.string()),
        *[pit_schema.field(column) for column in FEATURE_COLUMNS],
    ])


def _existing(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    import pyarrow.parquet as pq
    return pq.ParquetFile(path).read().to_pylist()


def write_rows(rows: list[dict[str, Any]], root: Path) -> dict[str, int]:
    import pyarrow as pa
    import pyarrow.parquet as pq
    grouped: dict[int, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(row["prediction_date"].year, []).append(row)
    for year, additions in grouped.items():
        path = root / f"partition_year={year}" / "part-00000.parquet"
        merged = {row["prediction_date"]: row for row in _existing(path)}
        merged.update({row["prediction_date"]: row for row in additions})
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        try:
            pq.write_table(pa.Table.from_pylist([merged[key] for key in sorted(merged)], schema=output_schema()), temporary,
                           compression="zstd", compression_level=6)
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
    return {"rows": len(rows), "files": len(grouped)}


def stats(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {"labels": len(rows), "feature_candidates": sum(row["feature_available"] for row in rows),
            "missing_feature_dates": [row["prediction_date"].isoformat() for row in rows if not row["feature_available"]],
            "class_distribution": dict(Counter(row["direction_class"] for row in rows)),
            "first_prediction_date": rows[0]["prediction_date"].isoformat(),
            "last_prediction_date": rows[-1]["prediction_date"].isoformat()}


def run(options: argparse.Namespace) -> dict[str, Any]:
    rows = build_rows(read_tx_daily(options.tx_root), read_features(options.features))
    return {"version": VERSION, **write_rows(rows, options.output), **stats(rows)}


if __name__ == "__main__":
    print(json.dumps(run(arguments()), ensure_ascii=False, indent=2))
