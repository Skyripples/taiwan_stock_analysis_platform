"""Build a leakage-checked next-session supervised learning dataset."""

from __future__ import annotations

import csv
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, Mapping

from config import PROJECT_ROOT


LOGGER = logging.getLogger("prediction_dataset")
HISTORY_DIR = PROJECT_ROOT / "data" / "history"
INPUT_PATH = HISTORY_DIR / "market_daily.csv"
OUTPUT_PATH = HISTORY_DIR / "prediction_dataset.csv"

DATE_FIELDS = (
    "taiwan_market_trade_date",
    "institutional_trade_date",
    "foreign_futures_trade_date",
    "night_futures_trade_date",
    "tsm_adr_trade_date",
    "sox_trade_date",
    "sp500_trade_date",
    "nasdaq_trade_date",
)

REQUIRED_FEATURE_FIELDS = (
    *DATE_FIELDS,
    "taiex_close",
    "taiex_change_percent",
    "tpex_close",
    "turnover",
    "advancing",
    "declining",
    "unchanged",
    "foreign_cash_flow",
    "foreign_futures_position",
    "night_futures_change",
    "tsm_adr_change_percent",
    "sox_change_percent",
    "sp500_change_percent",
    "nasdaq_change_percent",
)

OUTPUT_FIELDS = (
    "feature_date",
    "target_date",
    *REQUIRED_FEATURE_FIELDS,
    "next_taiex_close",
    "next_taiex_return",
    "target_direction",
)

INTEGER_FIELDS = {
    "turnover",
    "advancing",
    "declining",
    "unchanged",
    "foreign_cash_flow",
    "foreign_futures_position",
}


def build_prediction_dataset(
    input_path: Path = INPUT_PATH,
    output_path: Path = OUTPUT_PATH,
) -> int:
    """Validate history, pair adjacent trading sessions, and atomically write CSV."""

    history_rows = _read_history(input_path)
    dataset_rows = _build_rows(history_rows)
    _write_atomic(output_path, dataset_rows)
    return len(dataset_rows)


def _read_history(input_path: Path) -> list[Dict[str, str]]:
    try:
        source_file = input_path.open("r", encoding="utf-8", newline="")
    except FileNotFoundError as exc:
        raise ValueError(f"History file not found: {input_path}") from exc

    with source_file:
        reader = csv.DictReader(source_file)
        fields = tuple(reader.fieldnames or ())
        required = {"trade_date", *REQUIRED_FEATURE_FIELDS}
        if not required.issubset(fields):
            missing = sorted(required.difference(fields))
            raise ValueError(f"market_daily.csv is missing columns: {', '.join(missing)}")
        rows = list(reader)

    seen_dates: set[str] = set()
    parsed_dates = []
    for row_number, row in enumerate(rows, start=2):
        for field in required:
            if row.get(field) is None or not str(row[field]).strip():
                raise ValueError(f"Missing {field} at CSV row {row_number}")
        trade_date = _parse_date(row["trade_date"], f"trade_date at CSV row {row_number}")
        if trade_date in seen_dates:
            raise ValueError(f"Duplicate trade_date: {trade_date}")
        seen_dates.add(trade_date)
        parsed_dates.append(trade_date)
        _validate_feature_values(row, row_number)

    if parsed_dates != sorted(parsed_dates):
        raise ValueError("market_daily.csv trade_date order is not strictly ascending")
    return rows


def _validate_feature_values(row: Mapping[str, str], row_number: int) -> None:
    for field in DATE_FIELDS:
        _parse_date(row[field], f"{field} at CSV row {row_number}")
    for field in REQUIRED_FEATURE_FIELDS:
        if field in DATE_FIELDS:
            continue
        value = str(row[field]).strip()
        try:
            if field in INTEGER_FIELDS:
                int(value)
            else:
                float(value)
        except ValueError as exc:
            raise ValueError(f"Invalid numeric value for {field} at CSV row {row_number}") from exc
    if float(row["taiex_close"]) <= 0:
        raise ValueError(f"taiex_close must be positive at CSV row {row_number}")


def _build_rows(history_rows: list[Mapping[str, str]]) -> list[Dict[str, str | int | float]]:
    dataset_rows: list[Dict[str, str | int | float]] = []
    for index in range(max(0, len(history_rows) - 1)):
        feature = history_rows[index]
        target = history_rows[index + 1]
        feature_date = _parse_date(feature["trade_date"], "feature_date")
        target_date = _parse_date(target["trade_date"], "target_date")
        if target_date <= feature_date:
            raise ValueError(f"Invalid trading-date pair: {feature_date} -> {target_date}")

        for field in DATE_FIELDS:
            source_date = _parse_date(feature[field], field)
            latest_allowed_date = target_date if field == "night_futures_trade_date" else feature_date
            if source_date > latest_allowed_date:
                raise ValueError(
                    f"Potential data leakage: {field} {source_date} is later than "
                    f"its allowed date {latest_allowed_date}"
                )
        if feature["taiwan_market_trade_date"] != feature_date:
            raise ValueError(
                f"taiwan_market_trade_date must equal feature_date: {feature_date}"
            )

        taiex_close = float(feature["taiex_close"])
        next_taiex_close = float(target["taiex_close"])
        if taiex_close <= 0 or next_taiex_close <= 0:
            raise ValueError(f"TAIEX close must be positive for pair {feature_date} -> {target_date}")
        next_return = round((next_taiex_close / taiex_close - 1) * 100, 8)

        output_row: Dict[str, str | int | float] = {
            "feature_date": feature_date,
            "target_date": target_date,
        }
        for field in REQUIRED_FEATURE_FIELDS:
            output_row[field] = feature[field]
        output_row.update(
            {
                "next_taiex_close": _clean_number(next_taiex_close),
                "next_taiex_return": _clean_number(next_return),
                "target_direction": 1 if next_return > 0 else 0,
            }
        )
        dataset_rows.append(output_row)
    return dataset_rows


def _write_atomic(
    output_path: Path,
    rows: Iterable[Mapping[str, str | int | float]],
) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = output_path.with_suffix(f"{output_path.suffix}.tmp")
    try:
        with temporary_path.open("w", encoding="utf-8", newline="") as output_file:
            writer = csv.DictWriter(output_file, fieldnames=OUTPUT_FIELDS, extrasaction="raise")
            writer.writeheader()
            writer.writerows(rows)
        os.replace(temporary_path, output_path)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


def _parse_date(value: str, label: str) -> str:
    try:
        return datetime.strptime(str(value).strip(), "%Y-%m-%d").date().isoformat()
    except ValueError as exc:
        raise ValueError(f"Invalid date for {label}: {value}") from exc


def _clean_number(value: float) -> int | float:
    return int(value) if value.is_integer() else value


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(message)s")
    LOGGER.info("Prediction dataset build started")
    try:
        row_count = build_prediction_dataset()
    except (OSError, ValueError) as exc:
        LOGGER.error("Prediction dataset build failed: %s", exc)
        return 1
    if row_count == 0:
        LOGGER.warning("Prediction dataset contains only the header: at least two history rows are required")
    else:
        LOGGER.info("Prediction dataset written: %s", OUTPUT_PATH)
    LOGGER.info("Prediction dataset build finished | rows=%d", row_count)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
