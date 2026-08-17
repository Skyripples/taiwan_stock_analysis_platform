"""Record predictions and validate them after the next Taiwan trading close."""

from __future__ import annotations

import csv
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

from config import PROJECT_ROOT


LOGGER = logging.getLogger("prediction_history")
PREDICTION_PATH = PROJECT_ROOT / "data" / "prediction" / "prediction.json"
MARKET_HISTORY_PATH = PROJECT_ROOT / "data" / "history" / "market_daily.csv"
OUTPUT_PATH = PROJECT_ROOT / "data" / "history" / "prediction_history.csv"

FIELDS = (
    "feature_date",
    "target_date",
    "up_probability",
    "down_probability",
    "predicted_direction",
    "confidence",
    "model_version",
    "generated_at",
    "feature_close",
    "target_close",
    "actual_return",
    "actual_direction",
    "hit",
    "validated_at",
)


def update_prediction_history(
    prediction_path: Path = PREDICTION_PATH,
    market_history_path: Path = MARKET_HISTORY_PATH,
    output_path: Path = OUTPUT_PATH,
) -> tuple[int, int]:
    """Upsert today's prediction, settle pending rows, and atomically write CSV."""

    market_rows = _load_market_history(market_history_path)
    existing_rows = _load_existing_history(output_path)
    rows_by_date = {row["feature_date"]: row for row in existing_rows}

    prediction = _load_optional_prediction(prediction_path)
    if prediction is not None:
        prediction_row = _build_prediction_row(prediction, market_rows)
        existing = rows_by_date.get(prediction_row["feature_date"])
        if existing and existing.get("hit") in {"true", "false"}:
            LOGGER.info("Validated prediction already exists for %s; input was not duplicated", prediction_row["feature_date"])
        else:
            rows_by_date[prediction_row["feature_date"]] = prediction_row

    validated_count = 0
    ordered_dates = [row["trade_date"] for row in market_rows]
    market_by_date = {row["trade_date"]: row for row in market_rows}
    for feature_date, row in rows_by_date.items():
        if row.get("hit") in {"true", "false"}:
            continue
        if feature_date not in market_by_date:
            raise ValueError(f"Prediction feature_date is absent from market history: {feature_date}")
        feature_index = ordered_dates.index(feature_date)
        if feature_index + 1 >= len(ordered_dates):
            continue
        target_date = ordered_dates[feature_index + 1]
        configured_target = row.get("target_date", "")
        if configured_target and configured_target != target_date:
            raise ValueError(
                f"Prediction target_date does not match the next Taiwan trading day: "
                f"{configured_target} != {target_date}"
            )
        feature_close = float(market_by_date[feature_date]["taiex_close"])
        target_close = float(market_by_date[target_date]["taiex_close"])
        actual_return = round((target_close / feature_close - 1) * 100, 8)
        actual_direction = "up" if actual_return > 0 else "down"
        row.update(
            {
                "target_date": target_date,
                "feature_close": _clean_number(feature_close),
                "target_close": _clean_number(target_close),
                "actual_return": _clean_number(actual_return),
                "actual_direction": actual_direction,
                "hit": "true" if row["predicted_direction"] == actual_direction else "false",
                "validated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            }
        )
        validated_count += 1

    ordered_rows = [rows_by_date[key] for key in sorted(rows_by_date)]
    _write_atomic(output_path, ordered_rows)
    return len(ordered_rows), validated_count


def _load_market_history(path: Path) -> list[Dict[str, str]]:
    try:
        source = path.open("r", encoding="utf-8", newline="")
    except FileNotFoundError as exc:
        raise ValueError(f"Market history not found: {path}") from exc
    with source:
        reader = csv.DictReader(source)
        required = {"trade_date", "taiex_close"}
        missing = sorted(required.difference(reader.fieldnames or ()))
        if missing:
            raise ValueError(f"Market history is missing columns: {', '.join(missing)}")
        rows = list(reader)
    seen: set[str] = set()
    dates: list[str] = []
    for row_number, row in enumerate(rows, start=2):
        trade_date = _date(row.get("trade_date"), f"market history row {row_number}")
        if trade_date in seen:
            raise ValueError(f"Missing or duplicate trade_date at market history row {row_number}")
        try:
            close = float(str(row.get("taiex_close", "")).strip())
        except ValueError as exc:
            raise ValueError(f"Invalid taiex_close at market history row {row_number}") from exc
        if close <= 0:
            raise ValueError(f"taiex_close must be positive at market history row {row_number}")
        seen.add(trade_date)
        dates.append(trade_date)
        row["trade_date"] = trade_date
    if dates != sorted(dates):
        raise ValueError("Market history dates must be strictly ascending")
    return rows


def _load_existing_history(path: Path) -> list[Dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8", newline="") as source:
        reader = csv.DictReader(source)
        if tuple(reader.fieldnames or ()) != FIELDS:
            raise ValueError(f"Unexpected prediction history header: {path}")
        rows = list(reader)
    seen: set[str] = set()
    for row_number, row in enumerate(rows, start=2):
        feature_date = _date(row.get("feature_date"), f"prediction history row {row_number}")
        if feature_date in seen:
            raise ValueError(f"Missing or duplicate feature_date at prediction history row {row_number}")
        if row.get("target_date"):
            row["target_date"] = _date(row["target_date"], f"prediction target at row {row_number}")
        if row.get("hit") not in {"", "true", "false"}:
            raise ValueError(f"Invalid hit value at prediction history row {row_number}")
        seen.add(feature_date)
        row["feature_date"] = feature_date
    return rows


def _load_optional_prediction(path: Path) -> Dict[str, Any] | None:
    if not path.exists():
        LOGGER.info("Prediction file is not available; pending history will still be checked")
        return None
    try:
        with path.open("r", encoding="utf-8") as source:
            payload = json.load(source)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid prediction JSON: {path}") from exc
    if not isinstance(payload, dict):
        raise ValueError("Prediction payload must be an object")
    return payload


def _build_prediction_row(payload: Dict[str, Any], market_rows: list[Dict[str, str]]) -> Dict[str, Any]:
    feature_date = _date(payload.get("feature_date") or payload.get("prediction_date"), "prediction feature_date")
    target_date = _date(payload.get("target_date"), "prediction target_date") if payload.get("target_date") else ""
    direction = str(payload.get("direction") or "").strip().lower()
    model_version = str(payload.get("model_version") or "").strip()
    generated_at = str(payload.get("generated_at") or "").strip()
    if not feature_date or direction not in {"up", "down"} or not model_version or not generated_at:
        raise ValueError("Prediction is missing required fields")
    up_probability = _probability(payload.get("up_probability"), "up_probability")
    down_probability = _probability(payload.get("down_probability"), "down_probability")
    confidence = _probability(payload.get("confidence"), "confidence")
    if abs(up_probability + down_probability - 1) > 0.001:
        raise ValueError("Prediction probabilities must add up to 1")
    market_by_date = {row["trade_date"]: row for row in market_rows}
    if feature_date not in market_by_date:
        raise ValueError(f"Prediction feature_date is absent from market history: {feature_date}")
    return {
        "feature_date": feature_date,
        "target_date": target_date,
        "up_probability": up_probability,
        "down_probability": down_probability,
        "predicted_direction": direction,
        "confidence": confidence,
        "model_version": model_version,
        "generated_at": generated_at,
        "feature_close": _clean_number(float(market_by_date[feature_date]["taiex_close"])),
        "target_close": "",
        "actual_return": "",
        "actual_direction": "",
        "hit": "",
        "validated_at": "",
    }


def _probability(value: Any, label: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{label} must be a probability")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be a probability") from exc
    if number < 0 or number > 1:
        raise ValueError(f"{label} must be between 0 and 1")
    return number


def _date(value: Any, label: str) -> str:
    try:
        return datetime.strptime(str(value).strip(), "%Y-%m-%d").date().isoformat()
    except ValueError as exc:
        raise ValueError(f"Invalid date for {label}: {value}") from exc


def _write_atomic(path: Path, rows: list[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="") as output:
            writer = csv.DictWriter(output, fieldnames=FIELDS, extrasaction="raise")
            writer.writeheader()
            writer.writerows(rows)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _clean_number(value: float) -> int | float:
    return int(value) if value.is_integer() else value


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(message)s")
    LOGGER.info("Prediction history update started")
    try:
        row_count, validated_count = update_prediction_history()
    except (OSError, ValueError) as exc:
        LOGGER.error("Prediction history update failed: %s", exc)
        return 1
    LOGGER.info("Prediction history written: %s", OUTPUT_PATH)
    LOGGER.info("Prediction history update finished | rows=%d | newly_validated=%d", row_count, validated_count)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
