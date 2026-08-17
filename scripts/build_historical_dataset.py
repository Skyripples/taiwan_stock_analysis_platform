"""Build the supervised historical dataset from the isolated backfill output."""

from __future__ import annotations

import csv
import logging
import os
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable, Mapping

from config import PROJECT_ROOT


LOGGER = logging.getLogger("historical_prediction_dataset")
HISTORY_DIR = PROJECT_ROOT / "data" / "history"
INPUT_PATH = HISTORY_DIR / "backfill_market_daily.csv"
OUTPUT_PATH = HISTORY_DIR / "historical_prediction_dataset.csv"

SOURCE_DATE_FIELDS = (
    "taiwan_market_trade_date",
    "institutional_trade_date",
    "foreign_futures_trade_date",
    "night_futures_trade_date",
    "tsm_adr_trade_date",
    "sox_trade_date",
    "sp500_trade_date",
    "nasdaq_trade_date",
    "vix_trade_date",
    "us_treasury_observation_date",
    "usdtwd_trade_date",
    "dxy_trade_date",
    "nikkei_trade_date",
    "kospi_trade_date",
    "hang_seng_trade_date",
    "csi300_trade_date",
    "soxx_trade_date",
    "smh_trade_date",
    "nvda_trade_date",
    "amd_trade_date",
    "avgo_trade_date",
)

FEATURE_FIELDS = (
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
    "vix_change_percent",
    "us10y_change",
    "us2y_change",
    "yield_curve_10y_2y",
    "usdtwd_change_percent",
    "dxy_change_percent",
    "nikkei_change_percent",
    "kospi_change_percent",
    "hang_seng_change_percent",
    "csi300_change_percent",
    "soxx_change_percent",
    "smh_change_percent",
    "nvda_change_percent",
    "amd_change_percent",
    "avgo_change_percent",
)

INTEGER_FEATURE_FIELDS = {
    "turnover",
    "advancing",
    "declining",
    "unchanged",
    "foreign_cash_flow",
    "foreign_futures_position",
}

OUTPUT_FIELDS = (
    "feature_date",
    "target_date",
    *SOURCE_DATE_FIELDS,
    *FEATURE_FIELDS,
    "next_taiex_close",
    "next_taiex_return",
    "target_direction",
)


def build_historical_dataset(
    input_path: Path = INPUT_PATH,
    output_path: Path = OUTPUT_PATH,
) -> int:
    """Validate, pair adjacent completed sessions, and atomically write output."""

    history = _read_history(input_path)
    dataset = _build_dataset_rows(history)
    _write_atomic(output_path, dataset)
    return len(dataset)


def _read_history(path: Path) -> list[dict[str, str]]:
    try:
        handle = path.open("r", encoding="utf-8-sig", newline="")
    except FileNotFoundError as exc:
        raise ValueError(f"Historical backfill file not found: {path}") from exc

    required = {
        "trade_date",
        "prediction_target_date",
        *SOURCE_DATE_FIELDS,
        *FEATURE_FIELDS,
    }
    with handle:
        reader = csv.DictReader(handle)
        fieldnames = set(reader.fieldnames or ())
        missing_columns = sorted(required - fieldnames)
        if missing_columns:
            raise ValueError(
                "backfill_market_daily.csv is missing columns: "
                + ", ".join(missing_columns)
            )
        rows = list(reader)

    seen_dates: set[date] = set()
    ordered_dates: list[date] = []
    for row_number, row in enumerate(rows, start=2):
        for field in required:
            if row.get(field) is None or not str(row[field]).strip():
                raise ValueError(f"Missing {field} at CSV row {row_number}")

        trade_date = _parse_date(row["trade_date"], f"trade_date at CSV row {row_number}")
        if trade_date in seen_dates:
            raise ValueError(f"Duplicate trade_date: {trade_date.isoformat()}")
        seen_dates.add(trade_date)
        ordered_dates.append(trade_date)
        _validate_feature_row(row, trade_date, row_number)

    if ordered_dates != sorted(ordered_dates):
        raise ValueError(
            "backfill_market_daily.csv trade_date order is not strictly ascending"
        )
    return rows


def _validate_feature_row(
    row: Mapping[str, str], trade_date: date, row_number: int
) -> None:
    target_date = _parse_date(
        row["prediction_target_date"],
        f"prediction_target_date at CSV row {row_number}",
    )
    if target_date <= trade_date:
        raise ValueError(
            f"prediction_target_date must follow trade_date at CSV row {row_number}"
        )
    for field in SOURCE_DATE_FIELDS:
        source_date = _parse_date(row[field], f"{field} at CSV row {row_number}")
        if field == "night_futures_trade_date":
            valid = source_date == target_date
            rule = f"equal target date {target_date.isoformat()}"
        elif field in {
            "tsm_adr_trade_date",
            "sox_trade_date",
            "sp500_trade_date",
            "nasdaq_trade_date",
            "vix_trade_date",
            "us_treasury_observation_date",
            "usdtwd_trade_date",
            "dxy_trade_date",
            "nikkei_trade_date",
            "kospi_trade_date",
            "hang_seng_trade_date",
            "csi300_trade_date",
            "soxx_trade_date",
            "smh_trade_date",
            "nvda_trade_date",
            "amd_trade_date",
            "avgo_trade_date",
        }:
            valid = source_date < target_date
            rule = f"precede target date {target_date.isoformat()}"
        else:
            valid = source_date <= trade_date
            rule = f"not exceed feature date {trade_date.isoformat()}"
        if not valid:
            raise ValueError(
                f"Potential data leakage at CSV row {row_number}: "
                f"{field} {source_date.isoformat()} must {rule}"
            )
    if _parse_date(
        row["taiwan_market_trade_date"],
        f"taiwan_market_trade_date at CSV row {row_number}",
    ) != trade_date:
        raise ValueError(
            f"taiwan_market_trade_date must equal trade_date at CSV row {row_number}"
        )

    for field in FEATURE_FIELDS:
        text = str(row[field]).strip()
        try:
            value: int | float
            if field in INTEGER_FEATURE_FIELDS:
                value = int(text)
            else:
                value = float(text)
        except ValueError as exc:
            raise ValueError(
                f"Invalid numeric value for {field} at CSV row {row_number}"
            ) from exc
        if field in {"taiex_close", "tpex_close"} and value <= 0:
            raise ValueError(f"{field} must be positive at CSV row {row_number}")
        if field in {"turnover", "advancing", "declining", "unchanged"} and value < 0:
            raise ValueError(f"{field} must not be negative at CSV row {row_number}")


def _build_dataset_rows(
    history: list[Mapping[str, str]],
) -> list[dict[str, str | int | float]]:
    rows: list[dict[str, str | int | float]] = []
    history_by_date = {row["trade_date"]: row for row in history}
    for feature in history:
        feature_date = _parse_date(feature["trade_date"], "feature_date")
        target_date = _parse_date(
            feature["prediction_target_date"], "prediction_target_date"
        )
        if target_date <= feature_date:
            raise ValueError(
                f"Invalid trading-date pair: {feature_date} -> {target_date}"
            )
        target = history_by_date.get(target_date.isoformat())
        if target is None:
            # A source failure on the target session means its close is unknown;
            # never jump across that gap to a later completed row.
            continue

        feature_close = float(feature["taiex_close"])
        next_close = float(target["taiex_close"])
        if feature_close <= 0 or next_close <= 0:
            raise ValueError(
                f"TAIEX close must be positive for {feature_date} -> {target_date}"
            )
        next_return = round((next_close / feature_close - 1) * 100, 8)

        output: dict[str, str | int | float] = {
            "feature_date": feature_date.isoformat(),
            "target_date": target_date.isoformat(),
        }
        # Only the feature-date row is copied. No field from the target row is
        # exposed except its TAIEX close and the labels derived from that close.
        for field in (*SOURCE_DATE_FIELDS, *FEATURE_FIELDS):
            output[field] = feature[field]
        output.update(
            {
                "next_taiex_close": _clean_number(next_close),
                "next_taiex_return": _clean_number(next_return),
                "target_direction": 1 if next_return > 0 else 0,
            }
        )
        rows.append(output)
    return rows


def _write_atomic(
    path: Path, rows: Iterable[Mapping[str, Any]]
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(
                handle, fieldnames=OUTPUT_FIELDS, extrasaction="raise"
            )
            writer.writeheader()
            writer.writerows(rows)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _parse_date(value: Any, label: str) -> date:
    try:
        return datetime.strptime(str(value).strip(), "%Y-%m-%d").date()
    except ValueError as exc:
        raise ValueError(f"Invalid date for {label}: {value}") from exc


def _clean_number(value: float) -> int | float:
    return int(value) if value.is_integer() else value


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(message)s")
    LOGGER.info("Historical prediction dataset build started")
    try:
        row_count = build_historical_dataset()
    except (OSError, ValueError) as exc:
        LOGGER.error("Historical prediction dataset build failed: %s", exc)
        return 1
    if row_count == 0:
        LOGGER.warning("At least two complete backfill rows are required")
    else:
        LOGGER.info("Historical prediction dataset written: %s", OUTPUT_PATH)
    LOGGER.info("Historical prediction dataset build finished | rows=%d", row_count)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
