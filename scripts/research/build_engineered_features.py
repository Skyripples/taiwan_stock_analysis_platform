"""Build leakage-safe candidate features from the historical market dataset."""

from __future__ import annotations

import csv
import logging
import math
import os
import statistics
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable, Mapping

from config import PROJECT_ROOT


LOGGER = logging.getLogger("engineered_features")
HISTORY_DIR = PROJECT_ROOT / "data" / "history"
HISTORICAL_DATASET_PATH = HISTORY_DIR / "historical_prediction_dataset.csv"
BACKFILL_PATH = HISTORY_DIR / "backfill_market_daily.csv"
OUTPUT_PATH = HISTORY_DIR / "engineered_prediction_dataset.csv"

ENGINEERED_FIELDS = (
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

BACKFILL_NUMERIC_FIELDS = {
    "taiex_close",
    "turnover",
    "advancing",
    "declining",
    "foreign_cash_flow",
    "foreign_futures_position",
}


def build_engineered_features(
    historical_path: Path = HISTORICAL_DATASET_PATH,
    backfill_path: Path = BACKFILL_PATH,
    output_path: Path = OUTPUT_PATH,
) -> int:
    """Create an atomic, complete dataset using feature-date information only."""

    historical, original_fields = _read_historical_dataset(historical_path)
    backfill = _read_backfill(backfill_path)
    engineered_by_date = _calculate_features(backfill)

    rows: list[dict[str, Any]] = []
    for original in historical:
        feature_date = original["feature_date"]
        engineered = engineered_by_date.get(feature_date)
        # Warm-up rows and undefined ratios are excluded, never filled with zero.
        if engineered is None:
            continue
        output = dict(original)
        output.update(engineered)
        _validate_finite(output, feature_date)
        rows.append(output)

    _write_atomic(output_path, (*original_fields, *ENGINEERED_FIELDS), rows)
    return len(rows)


def _read_historical_dataset(path: Path) -> tuple[list[dict[str, str]], tuple[str, ...]]:
    rows, fields = _read_csv(path, "historical prediction dataset")
    required = {"feature_date", "target_date", "next_taiex_close", "next_taiex_return", "target_direction"}
    missing = sorted(required.difference(fields))
    if missing:
        raise ValueError("Historical dataset is missing columns: " + ", ".join(missing))
    _validate_ordered_dates(rows, "feature_date", "historical prediction dataset")
    for row_number, row in enumerate(rows, start=2):
        feature_date = _parse_date(row["feature_date"], f"feature_date at row {row_number}")
        target_date = _parse_date(row["target_date"], f"target_date at row {row_number}")
        if target_date <= feature_date:
            raise ValueError(f"target_date must follow feature_date at row {row_number}")
        if any(not str(value).strip() for value in row.values()):
            raise ValueError(f"Historical dataset contains a missing value at row {row_number}")
    return rows, fields


def _read_backfill(path: Path) -> list[dict[str, str]]:
    rows, fields = _read_csv(path, "backfill market history")
    required = {"trade_date", *BACKFILL_NUMERIC_FIELDS}
    missing = sorted(required.difference(fields))
    if missing:
        raise ValueError("Backfill history is missing columns: " + ", ".join(missing))
    _validate_ordered_dates(rows, "trade_date", "backfill market history")
    for row_number, row in enumerate(rows, start=2):
        for field in BACKFILL_NUMERIC_FIELDS:
            try:
                value = float(str(row[field]).strip())
            except (TypeError, ValueError) as exc:
                raise ValueError(f"Invalid {field} at backfill row {row_number}") from exc
            if not math.isfinite(value):
                raise ValueError(f"Non-finite {field} at backfill row {row_number}")
        if float(row["taiex_close"]) <= 0 or float(row["turnover"]) < 0:
            raise ValueError(f"Invalid market value at backfill row {row_number}")
    return rows


def _calculate_features(rows: list[Mapping[str, str]]) -> dict[str, dict[str, float]]:
    result: dict[str, dict[str, float]] = {}
    for index, row in enumerate(rows):
        # The longest lag is 20 sessions. Earlier rows are warm-up observations.
        if index < 20:
            continue
        window_5 = rows[index - 4 : index + 1]
        window_20 = rows[index - 19 : index + 1]
        close = _number(row, "taiex_close")
        turnover_mean = statistics.fmean(_number(item, "turnover") for item in window_20)
        cash_20 = [_number(item, "foreign_cash_flow") for item in window_20]
        cash_mean = statistics.fmean(cash_20)
        cash_std = statistics.pstdev(cash_20)
        advancing = _number(row, "advancing")
        declining = _number(row, "declining")
        breadth_total = advancing + declining
        if turnover_mean == 0 or cash_std == 0 or breadth_total == 0 or declining == 0:
            continue

        daily_returns = [
            (_number(rows[position], "taiex_close") / _number(rows[position - 1], "taiex_close") - 1) * 100
            for position in range(index - 19, index + 1)
        ]
        ma5 = statistics.fmean(_number(item, "taiex_close") for item in window_5)
        ma20 = statistics.fmean(_number(item, "taiex_close") for item in window_20)
        features = {
            "turnover_ratio_20d": _round(close_or_value=_number(row, "turnover") / turnover_mean),
            "market_breadth": _round(close_or_value=(advancing - declining) / breadth_total),
            "advance_decline_ratio": _round(close_or_value=advancing / declining),
            "foreign_cash_flow_5d_sum": _round(close_or_value=sum(_number(item, "foreign_cash_flow") for item in window_5)),
            "foreign_cash_flow_20d_zscore": _round(close_or_value=(_number(row, "foreign_cash_flow") - cash_mean) / cash_std),
            "foreign_futures_change_1d": _round(close_or_value=_number(row, "foreign_futures_position") - _number(rows[index - 1], "foreign_futures_position")),
            "foreign_futures_change_5d": _round(close_or_value=_number(row, "foreign_futures_position") - _number(rows[index - 5], "foreign_futures_position")),
            "taiex_return_3d": _return(close, _number(rows[index - 3], "taiex_close")),
            "taiex_return_5d": _return(close, _number(rows[index - 5], "taiex_close")),
            "taiex_return_20d": _return(close, _number(rows[index - 20], "taiex_close")),
            "taiex_ma5_distance": _return(close, ma5),
            "taiex_ma20_distance": _return(close, ma20),
            "taiex_volatility_20d": _round(close_or_value=statistics.pstdev(daily_returns)),
        }
        if all(math.isfinite(value) for value in features.values()):
            result[row["trade_date"]] = features
    return result


def _return(current: float, reference: float) -> float:
    if reference <= 0:
        raise ValueError("TAIEX return reference must be positive")
    return _round(close_or_value=(current / reference - 1) * 100)


def _round(*, close_or_value: float) -> float:
    return round(close_or_value, 10)


def _number(row: Mapping[str, str], field: str) -> float:
    return float(row[field])


def _read_csv(path: Path, label: str) -> tuple[list[dict[str, str]], tuple[str, ...]]:
    try:
        handle = path.open("r", encoding="utf-8-sig", newline="")
    except FileNotFoundError as exc:
        raise ValueError(f"{label.title()} not found: {path}") from exc
    with handle:
        reader = csv.DictReader(handle)
        fields = tuple(reader.fieldnames or ())
        if not fields:
            raise ValueError(f"{label.title()} has no header")
        return list(reader), fields


def _validate_ordered_dates(rows: list[Mapping[str, str]], field: str, label: str) -> None:
    previous: date | None = None
    seen: set[date] = set()
    for row_number, row in enumerate(rows, start=2):
        current = _parse_date(row.get(field), f"{field} at {label} row {row_number}")
        if current in seen or (previous is not None and current <= previous):
            raise ValueError(f"{label.title()} dates must be unique and strictly ascending")
        seen.add(current)
        previous = current


def _parse_date(value: Any, label: str) -> date:
    try:
        return datetime.strptime(str(value).strip(), "%Y-%m-%d").date()
    except ValueError as exc:
        raise ValueError(f"Invalid date for {label}: {value}") from exc


def _validate_finite(row: Mapping[str, Any], feature_date: str) -> None:
    for field in ENGINEERED_FIELDS:
        try:
            value = float(row[field])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"Invalid engineered feature {field} at {feature_date}") from exc
        if not math.isfinite(value):
            raise ValueError(f"Non-finite engineered feature {field} at {feature_date}")


def _write_atomic(path: Path, fields: Iterable[str], rows: Iterable[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="raise")
            writer.writeheader()
            writer.writerows(rows)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(message)s")
    LOGGER.info("Engineered feature build started")
    try:
        row_count = build_engineered_features()
    except (OSError, ValueError) as exc:
        LOGGER.error("Engineered feature build failed: %s", exc)
        return 1
    LOGGER.info("Engineered dataset written: %s", OUTPUT_PATH)
    LOGGER.info("Engineered feature build finished | rows=%d | new_features=%d", row_count, len(ENGINEERED_FIELDS))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
