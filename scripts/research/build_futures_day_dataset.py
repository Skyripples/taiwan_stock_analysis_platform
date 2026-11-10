"""Build the independent V5 DAY task: 08:15 cutoff, 08:45-to-regular-close TX label.

The old B4 dataset remains untouched.  This builder revalidates each selected
TX Core feature against an explicit 08:15 cutoff; it never changes an 08:30
timestamp string and calls that proof.
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
from collections import Counter
from datetime import date, datetime, time, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from config import PROJECT_ROOT
from research import build_futures_daily_pit_alignment as pit
from research.futures_binary import features, walk_forward
from research.futures_us_pre_post_targets import historical_front_contract
from research.futures_v5_task_definitions import DAY as DAY_DEFINITION

UTC = timezone.utc
TAIPEI = ZoneInfo("Asia/Taipei")
VERSION = "v5.day-1.0"
TASK = DAY_DEFINITION.key
TX_ROOT = PROJECT_ROOT / "data_lake" / "staging" / "futures_daily" / "product=TX"
FEATURE_ROOT = PROJECT_ROOT / "data_lake" / "futures_prediction" / "daily_features"
OUTPUT_ROOT = PROJECT_ROOT / "data_lake" / "futures_prediction" / "day_0815_dataset"
FEATURE_COLUMNS = features.feature_names()
TARGET_COLUMN = "day_regular_open_to_close_return"
BINARY_COLUMN = "binary_direction_target"
TARGET_AVAILABLE_AT_BASIS = "existing TX daily source convention: trade_date 15:30 Asia/Taipei; not an observed historical publication timestamp"

# Every non-null core value has explicit predecessor-source availability.  A
# null value remains a valid missing feature, rather than deleting the row.
FEATURE_AVAILABILITY = {
    "tx_previous_day_return": ("tx_available_at", "tx_prior_available_at"),
    "tx_open_to_close_return": ("tx_available_at",),
    "tx_intraday_range_pct": ("tx_available_at",),
    "tx_true_range_pct": ("tx_available_at", "tx_prior_available_at"),
    "tx_return_volatility_5d": ("tx_available_at", "tx_prior_available_at"),
    "tx_volume": ("tx_available_at",),
    "tx_volume_change_1d": ("tx_available_at", "tx_prior_available_at"),
    "tx_open_interest": ("tx_available_at",),
    "tx_open_interest_change_1d": ("tx_available_at", "tx_prior_available_at"),
    "tx_open_interest_change_pct": ("tx_available_at", "tx_prior_available_at"),
    "tx_days_to_expiry": ("tx_available_at",),
    "tx_expiry_week": ("tx_available_at",),
    "tx_roll_regime": ("tx_available_at", "tx_prior_available_at"),
}


def prediction_timestamp(day: date) -> datetime:
    return datetime.combine(day, time(8, 15), TAIPEI)


def _aware(value: Any, field: str) -> datetime:
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return parsed.astimezone(UTC)


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tx-root", type=Path, default=TX_ROOT)
    parser.add_argument("--features", type=Path, default=FEATURE_ROOT)
    parser.add_argument("--output", type=Path, default=OUTPUT_ROOT)
    parser.add_argument("--dry-run", action="store_true", help="Read and validate only; do not write Parquet.")
    return parser.parse_args()


def read_tx_daily(root: Path = TX_ROOT) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in sorted(root.glob("year=*/daily.jsonl.gz")):
        with gzip.open(path, "rt", encoding="utf-8") as source:
            for line in source:
                row = json.loads(line)
                if row.get("product") == "TX" and row.get("session") == "regular":
                    row["trade_date"] = date.fromisoformat(str(row["trade_date"])[:10])
                    row["available_at"] = _aware(row["available_at"], "available_at")
                    rows.append(row)
    return rows


def read_daily_features(root: Path = FEATURE_ROOT) -> dict[date, dict[str, Any]]:
    import pyarrow.parquet as pq
    output: dict[date, dict[str, Any]] = {}
    for path in sorted(root.rglob("*.parquet")):
        for row in pq.ParquetFile(path).read().to_pylist():
            day = row["prediction_date"]
            if day in output:
                raise ValueError(f"duplicate daily feature prediction_date: {day}")
            output[day] = row
    return output


def revalidate_tx_core_at_0815(day: date, source: dict[str, Any] | None) -> tuple[dict[str, Any], datetime | None, str | None]:
    """Project source feature values only after per-feature availability validation."""
    result = {name: None for name in FEATURE_COLUMNS}
    if source is None:
        return result, None, "missing_daily_feature_row"
    if source.get("prediction_date") != day:
        return result, None, "source_prediction_date_mismatch"
    source_prediction_at = _aware(source.get("prediction_at"), "source prediction_at")
    if source_prediction_at.astimezone(TAIPEI).time() != time(8, 30):
        return result, None, "unexpected_source_feature_cutoff"
    cutoff = prediction_timestamp(day).astimezone(UTC)
    used: list[datetime] = []
    for name in FEATURE_COLUMNS:
        if name not in source:
            return result, None, f"missing_source_feature_column:{name}"
        value = source[name]
        if value is None:
            continue
        requirements = FEATURE_AVAILABILITY[name]
        try:
            evidence = [_aware(source.get(field), field) for field in requirements]
        except (TypeError, ValueError):
            return result, None, f"missing_source_availability:{name}"
        if any(available > cutoff for available in evidence):
            return result, None, f"source_available_after_0815:{name}"
        result[name] = value
        used.extend(evidence)
    return result, max(used) if used else None, None


def _direction(value: float | None) -> str | None:
    if value is None:
        return None
    return "up" if value > 0 else "down" if value < 0 else "flat"


def build_rows(tx_rows: list[dict[str, Any]], source_features: dict[date, dict[str, Any]]) -> list[dict[str, Any]]:
    by_key = {(row["trade_date"], row.get("contract")): row for row in tx_rows}
    days = sorted({row["trade_date"] for row in tx_rows})
    output: list[dict[str, Any]] = []
    for day in days:
        cutoff = prediction_timestamp(day)
        selection = historical_front_contract(cutoff)
        selected = by_key.get((day, selection.contract)) if selection.contract else None
        target: float | None = None
        target_available_at: datetime | None = None
        target_reason: str | None = None
        if selected is None:
            target_reason = "missing_selected_contract_regular_daily_row" if selection.contract else "contract_rule_unavailable"
        else:
            try:
                open_price, close_price = float(selected["open"]), float(selected["close"])
                if open_price == 0:
                    target_reason = "invalid_target_open"
                else:
                    target = close_price / open_price - 1.0
                    target_available_at = _aware(selected["available_at"], "target available_at")
                    if target_available_at <= cutoff.astimezone(UTC):
                        raise ValueError("daily label availability must be after DAY cutoff")
            except (KeyError, TypeError, ValueError) as error:
                if target_reason is None:
                    target_reason = f"invalid_target_price:{error}"
                open_price = close_price = None
        projected, max_available, feature_reason = revalidate_tx_core_at_0815(day, source_features.get(day))
        direction = _direction(target)
        row: dict[str, Any] = {
            "partition_year": day.year, "task": TASK, "dataset_version": VERSION,
            "prediction_date": day, "prediction_at": cutoff,
            "target_contract": selection.contract, "target_contract_expiry_date": selection.expiry_date,
            "contract_selection_evidence_status": selection.evidence_status,
            "contract_selection_basis": json.dumps(selection.basis, ensure_ascii=False, sort_keys=True),
            "target_open": open_price if selected else None, "target_close": close_price if selected else None,
            TARGET_COLUMN: target, "direction_class": direction,
            BINARY_COLUMN: (1 if direction == "up" else 0 if direction == "down" else None),
            "target_available_at": target_available_at, "target_available_at_basis": TARGET_AVAILABLE_AT_BASIS,
            "target_missing_reason": target_reason,
            "feature_pit_status": "passed_0815" if feature_reason is None else "unavailable_0815",
            "feature_missing_reason": feature_reason,
            "max_feature_available_at": max_available,
            # A historical timestamp check is not evidence that an 08:15
            # snapshot was actually built and acquired on time.
            "live_readiness": "not_assessed_no_0815_live_snapshot",
        }
        row.update(projected)
        output.append(row)
    return output


def stats(rows: list[dict[str, Any]]) -> dict[str, Any]:
    labels = Counter(row["direction_class"] or "missing" for row in rows)
    candidates = [row for row in rows if row["feature_pit_status"] == "passed_0815"]
    binary = [row for row in candidates if row[BINARY_COLUMN] in {0, 1}]
    indexed = list(enumerate(candidates))
    folds = walk_forward.expanding_walk_forward_prepared(indexed, allow_resolved_flat=True)
    return {
        "rows": len(rows), "date_range": [rows[0]["prediction_date"].isoformat(), rows[-1]["prediction_date"].isoformat()] if rows else [],
        "label_distribution": dict(sorted(labels.items())), "feature_pit_passed": len(candidates),
        "binary_training_candidates": len(binary), "fold_count": len(folds),
        "test_sessions": sum(len(fold.test_indices) for fold in folds),
        "selection_evidence": dict(sorted(Counter(row["contract_selection_evidence_status"] for row in rows).items())),
    }


def output_schema():
    import pyarrow as pa
    return pa.schema([
        ("partition_year", pa.int64()), ("task", pa.string()), ("dataset_version", pa.string()),
        ("prediction_date", pa.date32()), ("prediction_at", pa.timestamp("us", tz="Asia/Taipei")),
        ("target_contract", pa.string()), ("target_contract_expiry_date", pa.date32()),
        ("contract_selection_evidence_status", pa.string()), ("contract_selection_basis", pa.string()),
        ("target_open", pa.float64()), ("target_close", pa.float64()), (TARGET_COLUMN, pa.float64()),
        ("direction_class", pa.string()), (BINARY_COLUMN, pa.int8()),
        ("target_available_at", pa.timestamp("us", tz="UTC")), ("target_available_at_basis", pa.string()),
        ("target_missing_reason", pa.string()), ("feature_pit_status", pa.string()),
        ("feature_missing_reason", pa.string()), ("max_feature_available_at", pa.timestamp("us", tz="UTC")),
        ("live_readiness", pa.string()),
        *[(name, pa.bool_() if name in {"tx_expiry_week", "tx_roll_regime"} else pa.float64()) for name in FEATURE_COLUMNS],
    ])


def write_rows(rows: list[dict[str, Any]], root: Path) -> None:
    import pyarrow as pa
    import pyarrow.parquet as pq
    by_year: dict[int, list[dict[str, Any]]] = {}
    for row in rows:
        by_year.setdefault(row["prediction_date"].year, []).append(row)
    for year, additions in by_year.items():
        path = root / f"partition_year={year}" / "part-00000.parquet"
        existing = pq.ParquetFile(path).read().to_pylist() if path.exists() else []
        merged = {row["prediction_date"]: row for row in existing}
        merged.update({row["prediction_date"]: row for row in additions})
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        try:
            pq.write_table(pa.Table.from_pylist([merged[key] for key in sorted(merged)], schema=output_schema()), temporary,
                           compression="zstd", compression_level=6)
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


def run(options: argparse.Namespace) -> dict[str, Any]:
    rows = build_rows(read_tx_daily(options.tx_root), read_daily_features(options.features))
    result = {"task": TASK, "version": VERSION, **stats(rows), "dry_run": bool(options.dry_run)}
    if not options.dry_run:
        write_rows(rows, options.output)
    return result


if __name__ == "__main__":
    print(json.dumps(run(arguments()), ensure_ascii=False, indent=2, default=str))
