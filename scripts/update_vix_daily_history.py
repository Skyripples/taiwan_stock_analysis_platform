"""Backfill VIX onto the small daily history using the formal pre-open rule."""

from __future__ import annotations

import csv
import os
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from config import PROJECT_ROOT
from trading_calendar import get_next_trading_day
from update_global_macro_history import _fetch_yahoo, _latest_before


PATH = PROJECT_ROOT / "data" / "history" / "market_daily.csv"


def update_vix_daily_history(path: Path = PATH) -> int:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        fields = tuple(reader.fieldnames or ())
        rows = list(reader)
    if not rows:
        raise ValueError("Daily market history is empty")
    first = date.fromisoformat(rows[0]["trade_date"])
    last = date.fromisoformat(rows[-1]["trade_date"])
    history = _fetch_yahoo("^VIX", first - timedelta(days=10), last + timedelta(days=10))
    output: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        if index + 1 < len(rows):
            target = date.fromisoformat(rows[index + 1]["trade_date"])
        else:
            target_text = get_next_trading_day(row["trade_date"])
            if target_text is None:
                raise ValueError(f"Trading calendar cannot resolve target after {row['trade_date']}")
            target = date.fromisoformat(target_text)
        source_date, observation = _latest_before(history, target)
        output.append({**row, "vix_trade_date": source_date.isoformat(), "vix_change_percent": observation["change_percent"]})
    output_fields = tuple(field for field in fields if field not in {"vix_trade_date", "vix_change_percent"}) + ("vix_trade_date", "vix_change_percent")
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(
                handle,
                fieldnames=output_fields,
                extrasaction="raise",
                lineterminator="\n",
            )
            writer.writeheader(); writer.writerows(output); handle.flush(); os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists(): temporary.unlink()
    return len(output)


if __name__ == "__main__":
    print(f"VIX daily history updated | rows={update_vix_daily_history()}")
