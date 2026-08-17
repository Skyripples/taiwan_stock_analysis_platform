"""Atomically add leakage-safe KOSPI observations to daily market history."""

from __future__ import annotations

import csv
import os
from datetime import date, timedelta
from pathlib import Path

from config import PROJECT_ROOT
from trading_calendar import get_next_trading_day
from update_global_macro_history import _fetch_yahoo, _latest_before


PATH = PROJECT_ROOT / "data" / "history" / "market_daily.csv"


def update_kospi_daily_history(path: Path = PATH) -> int:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        fields = tuple(reader.fieldnames or ())
        rows = list(reader)
    if not rows or "trade_date" not in fields:
        raise ValueError("Daily market history is empty or invalid")

    start = date.fromisoformat(rows[0]["trade_date"]) - timedelta(days=20)
    last_target = get_next_trading_day(rows[-1]["trade_date"])
    if last_target is None:
        raise ValueError("Trading Calendar cannot determine the latest target date")
    targets = [date.fromisoformat(rows[index + 1]["trade_date"]) for index in range(len(rows) - 1)]
    targets.append(date.fromisoformat(last_target))
    history = _fetch_yahoo("^KS11", start, targets[-1] + timedelta(days=1))

    output = []
    for row, target in zip(rows, targets):
        source_date, observation = _latest_before(history, target)
        if source_date >= target:
            raise ValueError(f"KOSPI temporal leakage at {row['trade_date']}")
        output.append({
            **row,
            "kospi_trade_date": source_date.isoformat(),
            "kospi_change_percent": observation["change_percent"],
        })
    output_fields = tuple(
        field for field in fields
        if field not in {"kospi_trade_date", "kospi_change_percent"}
    ) + ("kospi_trade_date", "kospi_change_percent")

    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(
                handle,
                fieldnames=output_fields,
                extrasaction="raise",
                lineterminator="\n",
            )
            writer.writeheader()
            writer.writerows(output)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()
    return len(output)


if __name__ == "__main__":
    print(f"KOSPI daily history updated | rows={update_kospi_daily_history()}")
