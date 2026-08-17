"""Atomically augment historical backfill rows with semiconductor candidates."""

from __future__ import annotations

import csv
import logging
import os
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from config import PROJECT_ROOT
from update_global_macro_history import _fetch_yahoo, _latest_before


LOGGER = logging.getLogger("semiconductor_history")
INPUT_PATH = PROJECT_ROOT / "data" / "history" / "backfill_market_daily.csv"
YAHOO_SERIES = {
    "soxx": "SOXX",
    "smh": "SMH",
    "nvda": "NVDA",
    "amd": "AMD",
    "avgo": "AVGO",
}
SOURCE_DATE_FIELDS = tuple(f"{name}_trade_date" for name in YAHOO_SERIES)
FEATURE_FIELDS = tuple(f"{name}_change_percent" for name in YAHOO_SERIES)


def update_semiconductor_history(path: Path = INPUT_PATH) -> dict[str, Any]:
    rows, fields = _read_rows(path)
    start = date.fromisoformat(rows[0]["trade_date"]) - timedelta(days=20)
    end = date.fromisoformat(rows[-1]["prediction_target_date"]) + timedelta(days=1)
    histories = {
        name: _fetch_yahoo(symbol, start, end)
        for name, symbol in YAHOO_SERIES.items()
    }
    output: list[dict[str, Any]] = []
    for row in rows:
        target = date.fromisoformat(row["prediction_target_date"])
        augmented: dict[str, Any] = dict(row)
        for name, history in histories.items():
            source_date, observation = _latest_before(history, target)
            if source_date >= target:
                raise ValueError(f"{name} temporal leakage at {row['trade_date']}")
            augmented[f"{name}_trade_date"] = source_date.isoformat()
            augmented[f"{name}_change_percent"] = observation["change_percent"]
        output.append(augmented)

    output_fields = (
        tuple(field for field in fields if field not in {*SOURCE_DATE_FIELDS, *FEATURE_FIELDS})
        + SOURCE_DATE_FIELDS
        + FEATURE_FIELDS
    )
    _write_atomic(path, output_fields, output)
    return {
        "rows": len(output),
        "feature_start": output[0]["trade_date"],
        "feature_end": output[-1]["trade_date"],
        "source_ranges": {
            name: {
                "start": min(row[f"{name}_trade_date"] for row in output),
                "end": max(row[f"{name}_trade_date"] for row in output),
            }
            for name in YAHOO_SERIES
        },
    }


def _read_rows(path: Path) -> tuple[list[dict[str, str]], tuple[str, ...]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        fields = tuple(reader.fieldnames or ())
        rows = list(reader)
    if not rows or not {"trade_date", "prediction_target_date"}.issubset(fields):
        raise ValueError("Backfill market history is empty or invalid")
    return rows, fields


def _write_atomic(path: Path, fields: tuple[str, ...], rows: list[dict[str, Any]]) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="raise", lineterminator="\n")
            writer.writeheader(); writer.writerows(rows); handle.flush(); os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists(): temporary.unlink()


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(message)s")
    try:
        result = update_semiconductor_history()
    except (OSError, ValueError) as exc:
        LOGGER.error("Semiconductor history update failed: %s", exc)
        return 1
    LOGGER.info(
        "Semiconductor history updated | rows=%d | %s to %s",
        result["rows"], result["feature_start"], result["feature_end"],
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
