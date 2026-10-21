"""Build next-session TX regular-session targets from PIT-safe daily features."""

from __future__ import annotations

import argparse
import json
import os
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Iterable

from config import PROJECT_ROOT
from research import build_futures_daily_pit_alignment as feature_builder
from trading_calendar import get_trading_days_in_range


VERSION = "v4.6-1.0"
DEFAULT_START = date(2021, 10, 14)
FEATURE_ROOT = feature_builder.OUTPUT_ROOT
OUTPUT_ROOT = PROJECT_ROOT / "data_lake" / "futures_prediction" / "daily_targets"


@dataclass(frozen=True)
class DirectionThresholds:
    down_max: float = -0.002
    up_min: float = 0.002

    def __post_init__(self) -> None:
        if self.down_max >= self.up_min:
            raise ValueError("down_max must be less than up_min")


DEFAULT_THRESHOLDS = DirectionThresholds()


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build next-session TX daily targets")
    parser.add_argument("--start", type=date.fromisoformat, default=DEFAULT_START)
    parser.add_argument(
        "--end", type=lambda value: date.today() if value == "today" else date.fromisoformat(value),
        default=date.today(),
    )
    parser.add_argument("--features", type=Path, default=FEATURE_ROOT)
    parser.add_argument("--output", type=Path, default=OUTPUT_ROOT)
    parser.add_argument("--neutral-threshold", type=float, default=DEFAULT_THRESHOLDS.up_min)
    options = parser.parse_args()
    if options.start > options.end:
        parser.error("--start cannot exceed --end")
    if options.neutral_threshold < 0:
        parser.error("--neutral-threshold cannot be negative")
    return options


def classify_direction(value: float | None, thresholds: DirectionThresholds = DEFAULT_THRESHOLDS) -> str | None:
    if value is None:
        return None
    if value >= thresholds.up_min:
        return "up"
    if value <= thresholds.down_max:
        return "down"
    return "neutral"


def read_features(root: Path, start: date, end: date) -> list[dict[str, Any]]:
    import pyarrow.parquet as pq

    rows: list[dict[str, Any]] = []
    for path in sorted(root.rglob("*.parquet")):
        for row in pq.ParquetFile(path).read().to_pylist():
            prediction_date = row["prediction_date"]
            if start <= prediction_date <= end:
                rows.append(row)
    return rows


def build_targets(
    sessions: list[date], feature_rows: Iterable[dict[str, Any]], tx_rows: Iterable[dict[str, Any]],
    thresholds: DirectionThresholds = DEFAULT_THRESHOLDS,
) -> list[dict[str, Any]]:
    front_tx = feature_builder.tx_by_date(tx_rows, "front")
    session_index = {day: index for index, day in enumerate(sessions)}
    output: list[dict[str, Any]] = []
    seen: set[date] = set()

    for feature in sorted(feature_rows, key=lambda row: row["prediction_date"]):
        prediction_date = feature["prediction_date"]
        if prediction_date in seen:
            raise ValueError(f"Duplicate feature prediction_date: {prediction_date}")
        seen.add(prediction_date)
        prediction_at = feature_builder.aware_datetime(feature["prediction_at"])
        max_available = feature.get("max_feature_available_at")
        if max_available is not None and feature_builder.aware_datetime(max_available) > prediction_at:
            raise ValueError(f"Feature leakage at {prediction_date}: max_feature_available_at > prediction_at")
        index = session_index.get(prediction_date)
        if index is None:
            raise ValueError(f"Prediction date is not an official trading session: {prediction_date}")

        flags: dict[str, Any] = {"missing_target_session_data": False, "target_roll_regime": False}
        next_session = sessions[index + 1] if index + 1 < len(sessions) else None
        target = front_tx.get(next_session) if next_session else None
        target_return: float | None = None
        target_contract = None
        target_available_at = None
        if target is None:
            flags["missing_target_session_data"] = True
        else:
            target_return = feature_builder.ratio_change(target.get("close"), target.get("open"))
            target_contract = target.get("contract")
            target_available_at = feature_builder.aware_datetime(target["available_at"])
            flags["target_roll_regime"] = bool(
                feature.get("tx_contract") != target_contract or target.get("is_expiry_week")
            )

        output.append({
            "partition_year": prediction_date.year,
            "prediction_date": prediction_date,
            "prediction_at": prediction_at,
            "feature_version": feature.get("feature_version"),
            "tx_source_trading_date": feature.get("tx_source_trading_date"),
            "max_feature_available_at": max_available,
            "feature_quality_flags": feature.get("quality_flags"),
            "target_version": VERSION,
            "target_trading_date": next_session,
            "target_contract": target_contract,
            "target_available_at": target_available_at,
            "next_trading_day_regular_return": target_return,
            "direction_class": classify_direction(target_return, thresholds),
            "direction_down_max": thresholds.down_max,
            "direction_up_min": thresholds.up_min,
            "quality_flags": json.dumps(flags, separators=(",", ":"), sort_keys=True),
        })
    return output


def output_schema() -> Any:
    import pyarrow as pa

    return pa.schema([
        ("partition_year", pa.int64()), ("prediction_date", pa.date32()),
        ("prediction_at", pa.timestamp("us", tz="Asia/Taipei")),
        ("feature_version", pa.string()), ("tx_source_trading_date", pa.date32()),
        ("max_feature_available_at", pa.timestamp("us", tz="UTC")),
        ("feature_quality_flags", pa.string()), ("target_version", pa.string()),
        ("target_trading_date", pa.date32()), ("target_contract", pa.string()),
        ("target_available_at", pa.timestamp("us", tz="UTC")),
        ("next_trading_day_regular_return", pa.float64()), ("direction_class", pa.string()),
        ("direction_down_max", pa.float64()), ("direction_up_min", pa.float64()),
        ("quality_flags", pa.string()),
    ])


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
            pq.write_table(
                table, temporary, compression="zstd", compression_level=6,
                use_dictionary=["feature_version", "feature_quality_flags", "target_version", "target_contract",
                                "direction_class", "quality_flags"],
            )
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
    return {"rows": len(rows), "files": len(groups)}


def run(options: argparse.Namespace) -> dict[str, Any]:
    sessions, _ = get_trading_days_in_range(options.start - timedelta(days=14), options.end + timedelta(days=31))
    features = read_features(options.features, options.start, options.end)
    threshold = options.neutral_threshold
    rows = build_targets(
        sessions, features, feature_builder.read_tx_daily(), DirectionThresholds(-threshold, threshold),
    )
    return {"version": VERSION, "start": options.start.isoformat(), "end": options.end.isoformat(),
            "threshold": threshold, **write_rows(rows, options.output)}


def main() -> int:
    print(json.dumps(run(arguments()), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
