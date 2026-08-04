"""Upsert validated daily market data and signals into historical CSV files."""

from __future__ import annotations

import csv
import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping

from config import MARKET_DATA_DIR, PROJECT_ROOT


LOGGER = logging.getLogger("market_history")
HISTORY_DIR = PROJECT_ROOT / "data" / "history"
MARKET_HISTORY_PATH = HISTORY_DIR / "market_daily.csv"
SIGNALS_HISTORY_PATH = HISTORY_DIR / "signals_daily.csv"

SOURCE_FILES = {
    "taiwan_market": "taiwan_market_overview.json",
    "institutional": "institutional_investors.json",
    "foreign_futures": "foreign_futures_position.json",
    "night_futures": "night_futures.json",
    "tsm_adr": "tsm_adr.json",
    "sox": "sox_index.json",
    "sp500": "sp500_index.json",
    "nasdaq": "nasdaq_index.json",
    "signals": "market_signals.json",
}

SOURCE_DATE_FIELDS = (
    "taiwan_market_trade_date",
    "institutional_trade_date",
    "foreign_futures_trade_date",
    "night_futures_trade_date",
    "tsm_adr_trade_date",
    "sox_trade_date",
    "sp500_trade_date",
    "nasdaq_trade_date",
)

MARKET_FIELDS = (
    "trade_date",
    *SOURCE_DATE_FIELDS,
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

SIGNAL_NAMES = (
    "foreign_cash_flow",
    "foreign_futures_position",
    "night_futures",
    "tsm_adr",
    "sox_index",
)

SIGNALS_FIELDS = (
    "trade_date",
    *SOURCE_DATE_FIELDS,
    *(field for name in SIGNAL_NAMES for field in (f"{name}_status", f"{name}_score")),
    "market_score",
    "max_score",
    "percentage",
    "market_status",
)

EXPECTED_SIGNAL_VALUES = {
    "foreign_cash_flow": ("institutional", "foreign_and_mainland_investors", "net"),
    "foreign_futures_position": ("foreign_futures", "net_position", "open_interest"),
    "night_futures": ("night_futures", "change"),
    "tsm_adr": ("tsm_adr", "change"),
    "sox_index": ("sox", "change_percent"),
}

SCORE_BY_STATUS = {"bullish": 1, "neutral": 0, "bearish": -1}


def load_sources(market_data_dir: Path = MARKET_DATA_DIR) -> Dict[str, Any]:
    """Load every required JSON before any history file is written."""

    loaded: Dict[str, Any] = {}
    for source_name, filename in SOURCE_FILES.items():
        path = market_data_dir / filename
        try:
            with path.open("r", encoding="utf-8") as source_file:
                loaded[source_name] = json.load(source_file)
        except FileNotFoundError as exc:
            raise ValueError(f"Required market data file not found: {path}") from exc
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid JSON in market data file: {path}") from exc
    return loaded


def build_history_rows(sources: Mapping[str, Any]) -> tuple[Dict[str, Any], Dict[str, Any]]:
    """Validate aligned source data and build both CSV rows in memory."""

    records = {
        name: _latest_record(sources.get(name), name)
        for name in SOURCE_FILES
        if name != "signals"
    }
    dates = {name: _require_date(record.get("trade_date"), name) for name, record in records.items()}
    trade_date = dates["taiwan_market"]
    source_dates = {
        "taiwan_market_trade_date": dates["taiwan_market"],
        "institutional_trade_date": dates["institutional"],
        "foreign_futures_trade_date": dates["foreign_futures"],
        "night_futures_trade_date": dates["night_futures"],
        "tsm_adr_trade_date": dates["tsm_adr"],
        "sox_trade_date": dates["sox"],
        "sp500_trade_date": dates["sp500"],
        "nasdaq_trade_date": dates["nasdaq"],
    }

    market_row: Dict[str, Any] = {
        "trade_date": trade_date,
        **source_dates,
        "taiex_close": _require_number(records["taiwan_market"], ("taiex", "close"), "taiwan_market"),
        "taiex_change_percent": _require_number(records["taiwan_market"], ("taiex", "change_percent"), "taiwan_market"),
        "tpex_close": _require_number(records["taiwan_market"], ("tpex", "close"), "taiwan_market"),
        "turnover": _require_integer(records["taiwan_market"], ("turnover",), "taiwan_market"),
        "advancing": _require_integer(records["taiwan_market"], ("advancing",), "taiwan_market"),
        "declining": _require_integer(records["taiwan_market"], ("declining",), "taiwan_market"),
        "unchanged": _require_integer(records["taiwan_market"], ("unchanged",), "taiwan_market"),
        "foreign_cash_flow": _require_integer(records["institutional"], ("foreign_and_mainland_investors", "net"), "institutional"),
        "foreign_futures_position": _require_integer(records["foreign_futures"], ("net_position", "open_interest"), "foreign_futures"),
        "night_futures_change": _require_number(records["night_futures"], ("change",), "night_futures"),
        "tsm_adr_change_percent": _require_number(records["tsm_adr"], ("change_percent",), "tsm_adr"),
        "sox_change_percent": _require_number(records["sox"], ("change_percent",), "sox"),
        "sp500_change_percent": _require_number(records["sp500"], ("change_percent",), "sp500"),
        "nasdaq_change_percent": _require_number(records["nasdaq"], ("change_percent",), "nasdaq"),
    }

    signal_payload = sources.get("signals")
    if not isinstance(signal_payload, dict) or not isinstance(signal_payload.get("signals"), dict):
        raise ValueError("Invalid market_signals payload")
    signals = signal_payload["signals"]
    if set(signals) != set(SIGNAL_NAMES):
        raise ValueError("market_signals must contain exactly the configured five signals")

    signals_row: Dict[str, Any] = {"trade_date": trade_date, **source_dates}
    for signal_name in SIGNAL_NAMES:
        signal = signals.get(signal_name)
        if not isinstance(signal, dict):
            raise ValueError(f"Invalid signal: {signal_name}")
        status = signal.get("status")
        score = signal.get("score")
        if status not in SCORE_BY_STATUS or score != SCORE_BY_STATUS[status]:
            raise ValueError(f"Invalid status or score for signal: {signal_name}")
        expected_path = EXPECTED_SIGNAL_VALUES[signal_name]
        expected_value = _require_number(
            records[expected_path[0]], expected_path[1:], expected_path[0]
        )
        if signal.get("value") != expected_value:
            raise ValueError(f"Signal value is stale or mismatched: {signal_name}")
        signals_row[f"{signal_name}_status"] = status
        signals_row[f"{signal_name}_score"] = score

    market_score = signal_payload.get("market_score")
    if not isinstance(market_score, dict):
        raise ValueError("market_signals is missing market_score")
    score_sum = sum(signals_row[f"{name}_score"] for name in SIGNAL_NAMES)
    score = _required_scalar_integer(market_score.get("score"), "market_score.score")
    max_score = _required_scalar_integer(market_score.get("max_score"), "market_score.max_score")
    percentage = _required_scalar_number(market_score.get("percentage"), "market_score.percentage")
    market_status = market_score.get("status")
    if score != score_sum or max_score != len(SIGNAL_NAMES):
        raise ValueError("Market Score does not match its signal scores")
    if not isinstance(market_status, str) or not market_status:
        raise ValueError("market_score.status is missing")
    signals_row.update(
        {
            "market_score": score,
            "max_score": max_score,
            "percentage": percentage,
            "market_status": market_status,
        }
    )
    return market_row, signals_row


def update_history(
    market_data_dir: Path = MARKET_DATA_DIR,
    history_dir: Path = HISTORY_DIR,
) -> tuple[Path, Path]:
    """Upsert today's complete rows and atomically replace both CSV files."""

    sources = load_sources(market_data_dir)
    market_row, signals_row = build_history_rows(sources)
    market_path = history_dir / MARKET_HISTORY_PATH.name
    signals_path = history_dir / SIGNALS_HISTORY_PATH.name

    market_rows = _upsert_rows(_read_existing(market_path, MARKET_FIELDS), market_row)
    signals_rows = _upsert_rows(_read_existing(signals_path, SIGNALS_FIELDS), signals_row)

    history_dir.mkdir(parents=True, exist_ok=True)
    market_temp = _write_temporary(market_path, MARKET_FIELDS, market_rows)
    signals_temp = _write_temporary(signals_path, SIGNALS_FIELDS, signals_rows)
    try:
        os.replace(market_temp, market_path)
        os.replace(signals_temp, signals_path)
    finally:
        for temporary_path in (market_temp, signals_temp):
            if temporary_path.exists():
                temporary_path.unlink()
    return market_path, signals_path


def _read_existing(path: Path, fields: tuple[str, ...]) -> list[Dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8", newline="") as source_file:
        reader = csv.DictReader(source_file)
        if tuple(reader.fieldnames or ()) != fields:
            raise ValueError(f"Unexpected CSV header: {path}")
        rows = list(reader)

    seen_dates: set[str] = set()
    for row in rows:
        trade_date = _require_date(row.get("trade_date"), str(path))
        if trade_date in seen_dates:
            raise ValueError(f"Duplicate trade_date in existing CSV: {trade_date}")
        seen_dates.add(trade_date)
    return rows


def _upsert_rows(rows: Iterable[Mapping[str, Any]], new_row: Mapping[str, Any]) -> list[Dict[str, Any]]:
    indexed = {str(row["trade_date"]): dict(row) for row in rows}
    indexed[str(new_row["trade_date"])] = dict(new_row)
    return [indexed[trade_date] for trade_date in sorted(indexed)]


def _write_temporary(
    output_path: Path,
    fields: tuple[str, ...],
    rows: Iterable[Mapping[str, Any]],
) -> Path:
    temporary_path = output_path.with_suffix(f"{output_path.suffix}.tmp")
    try:
        with temporary_path.open("w", encoding="utf-8", newline="") as output_file:
            writer = csv.DictWriter(output_file, fieldnames=fields, extrasaction="raise")
            writer.writeheader()
            writer.writerows(rows)
        return temporary_path
    except Exception:
        if temporary_path.exists():
            temporary_path.unlink()
        raise


def _latest_record(payload: Any, source_name: str) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError(f"Invalid payload: {source_name}")
    data = payload.get("data")
    records = data.get("records") if isinstance(data, dict) else None
    if not isinstance(records, list) or len(records) != 1 or not isinstance(records[0], dict):
        raise ValueError(f"Expected exactly one record: {source_name}")
    return records[0]


def _require_date(value: Any, source_name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"Missing trade_date: {source_name}")
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as exc:
        raise ValueError(f"Invalid trade_date in {source_name}: {value}") from exc
    return parsed.isoformat()


def _require_integer(record: Mapping[str, Any], path: tuple[str, ...], source: str) -> int:
    value = _nested_value(record, path, source)
    return _required_scalar_integer(value, f"{source}.{'.'.join(path)}")


def _require_number(record: Mapping[str, Any], path: tuple[str, ...], source: str) -> int | float:
    value = _nested_value(record, path, source)
    return _required_scalar_number(value, f"{source}.{'.'.join(path)}")


def _nested_value(record: Mapping[str, Any], path: tuple[str, ...], source: str) -> Any:
    value: Any = record
    for key in path:
        if not isinstance(value, Mapping) or key not in value:
            raise ValueError(f"Missing {source}.{'.'.join(path)}")
        value = value[key]
    return value


def _required_scalar_integer(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{label} must be an integer")
    return value


def _required_scalar_number(value: Any, label: str) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be a number")
    return value


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(message)s")
    LOGGER.info("Market history update started")
    try:
        market_path, signals_path = update_history()
    except (OSError, ValueError) as exc:
        LOGGER.error("Market history update failed: %s", exc)
        return 1
    LOGGER.info("Market history written: %s", market_path)
    LOGGER.info("Signal history written: %s", signals_path)
    LOGGER.info("Market history update finished")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
