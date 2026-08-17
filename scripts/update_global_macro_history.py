"""Atomically augment historical backfill rows with pre-open global macro data."""

from __future__ import annotations

import csv
import io
import json
import logging
import math
import os
import time
from bisect import bisect_left
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests

from config import PROJECT_ROOT


LOGGER = logging.getLogger("global_macro_history")
INPUT_PATH = PROJECT_ROOT / "data" / "history" / "backfill_market_daily.csv"
YAHOO_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
FRED_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv"
YAHOO_SERIES = {"vix": "^VIX", "usdtwd": "TWD=X", "dxy": "DX-Y.NYB"}
SOURCE_DATE_FIELDS = (
    "vix_trade_date", "us_treasury_observation_date", "usdtwd_trade_date", "dxy_trade_date",
)
MACRO_FIELDS = (
    "vix_change_percent", "us10y_change", "us2y_change",
    "yield_curve_10y_2y", "usdtwd_change_percent", "dxy_change_percent",
)


def update_global_macro_history(path: Path = INPUT_PATH) -> dict[str, Any]:
    rows, fields = _read_rows(path)
    start = date.fromisoformat(rows[0]["trade_date"]) - timedelta(days=20)
    end = date.fromisoformat(rows[-1]["prediction_target_date"]) + timedelta(days=1)
    yahoo = {name: _fetch_yahoo(symbol, start, end) for name, symbol in YAHOO_SERIES.items()}
    yields = _fetch_fred_yields(start, end)
    output = []
    missing: dict[str, int] = {field: 0 for field in (*SOURCE_DATE_FIELDS, *MACRO_FIELDS)}
    for row in rows:
        target = date.fromisoformat(row["prediction_target_date"])
        try:
            vix_date, vix = _latest_before(yahoo["vix"], target)
            fx_date, fx = _latest_before(yahoo["usdtwd"], target)
            dxy_date, dxy = _latest_before(yahoo["dxy"], target)
            yield_date, yield_row = _latest_before(yields, target)
            augmented = {
                **row,
                "vix_trade_date": vix_date.isoformat(),
                "us_treasury_observation_date": yield_date.isoformat(),
                "usdtwd_trade_date": fx_date.isoformat(),
                "dxy_trade_date": dxy_date.isoformat(),
                "vix_change_percent": vix["change_percent"],
                "us10y_change": yield_row["us10y_change"],
                "us2y_change": yield_row["us2y_change"],
                "yield_curve_10y_2y": yield_row["yield_curve_10y_2y"],
                "usdtwd_change_percent": fx["change_percent"],
                "dxy_change_percent": dxy["change_percent"],
            }
        except ValueError as exc:
            LOGGER.warning("Macro row unavailable for %s: %s", row["trade_date"], exc)
            for field in missing:
                missing[field] += 1
            continue
        if any(date.fromisoformat(augmented[field]) >= target for field in SOURCE_DATE_FIELDS):
            raise ValueError(f"Macro temporal leakage detected for {row['trade_date']}")
        output.append(augmented)
    if len(output) != len(rows):
        raise ValueError(f"Macro data incomplete: complete={len(output)} required={len(rows)}; existing CSV was preserved")
    output_fields = tuple(field for field in fields if field not in {*SOURCE_DATE_FIELDS, *MACRO_FIELDS}) + SOURCE_DATE_FIELDS + MACRO_FIELDS
    _write_atomic(path, output_fields, output)
    return {"rows": len(output), "missing": missing, "start": output[0]["trade_date"], "end": output[-1]["trade_date"]}


def _fetch_yahoo(symbol: str, start: date, end: date) -> dict[date, dict[str, float]]:
    payload = _request_json(
        YAHOO_URL.format(symbol=requests.utils.quote(symbol, safe="")),
        params={
            "period1": int(datetime.combine(start, datetime.min.time(), tzinfo=timezone.utc).timestamp()),
            "period2": int(datetime.combine(end + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc).timestamp()),
            "interval": "1d", "events": "history",
        },
    )
    try:
        result = payload["chart"]["result"][0]
        timestamps = result["timestamp"]
        closes = result["indicators"]["quote"][0]["close"]
    except (KeyError, IndexError, TypeError) as exc:
        raise ValueError(f"Yahoo {symbol} response format changed") from exc
    history: dict[date, dict[str, float]] = {}
    previous: float | None = None
    for timestamp, value in zip(timestamps, closes):
        if value is None:
            continue
        close = float(value)
        observation = datetime.fromtimestamp(int(timestamp), timezone.utc).date()
        if previous is not None and previous > 0:
            history[observation] = {"close": close, "change_percent": (close / previous - 1) * 100}
        previous = close
    if not history:
        raise ValueError(f"Yahoo {symbol} returned no completed history")
    return history


def _fetch_fred_yields(start: date, end: date) -> dict[date, dict[str, float]]:
    series: dict[str, dict[date, float]] = {}
    for name in ("DGS2", "DGS10"):
        response = _request(FRED_URL, params={"id": name, "cosd": start.isoformat(), "coed": end.isoformat()})
        reader = csv.DictReader(io.StringIO(response.text))
        values: dict[date, float] = {}
        for row in reader:
            raw = row.get(name)
            if raw in (None, "", "."):
                continue
            values[date.fromisoformat(row["observation_date"])] = float(raw)
        if not values:
            raise ValueError(f"FRED {name} returned no observations")
        series[name] = values
    common = sorted(set(series["DGS2"]) & set(series["DGS10"]))
    history: dict[date, dict[str, float]] = {}
    previous: tuple[float, float] | None = None
    for observation in common:
        two, ten = series["DGS2"][observation], series["DGS10"][observation]
        if previous is not None:
            history[observation] = {
                "us2y_change": two - previous[0],
                "us10y_change": ten - previous[1],
                "yield_curve_10y_2y": ten - two,
            }
        previous = (two, ten)
    return history


def _latest_before(history: dict[date, dict[str, float]], target: date) -> tuple[date, dict[str, float]]:
    dates = sorted(history)
    index = bisect_left(dates, target) - 1
    if index < 0:
        raise ValueError(f"No observation before {target}")
    return dates[index], history[dates[index]]


def _request(url: str, **kwargs: Any) -> requests.Response:
    error: Exception | None = None
    for attempt in range(3):
        try:
            response = requests.get(url, timeout=45, headers={"User-Agent": "taiwan-stock-analysis-platform/1.0"}, **kwargs)
            response.raise_for_status()
            return response
        except requests.RequestException as exc:
            error = exc
            if attempt < 2:
                time.sleep(2**attempt)
    raise ValueError(f"Public data request failed: {error}")


def _request_json(url: str, **kwargs: Any) -> Any:
    try:
        return _request(url, **kwargs).json()
    except requests.JSONDecodeError as exc:
        raise ValueError("Public data response is not valid JSON") from exc


def _read_rows(path: Path) -> tuple[list[dict[str, str]], tuple[str, ...]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        fields = tuple(reader.fieldnames or ())
        rows = list(reader)
    if not rows or "trade_date" not in fields or "prediction_target_date" not in fields:
        raise ValueError("Backfill market history is empty or invalid")
    return rows, fields


def _write_atomic(path: Path, fields: tuple[str, ...], rows: list[dict[str, Any]]) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="raise")
            writer.writeheader(); writer.writerows(rows); handle.flush(); os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists(): temporary.unlink()


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(message)s")
    try:
        result = update_global_macro_history()
    except (OSError, ValueError) as exc:
        LOGGER.error("Global macro history update failed: %s", exc)
        return 1
    LOGGER.info("Global macro history updated | rows=%d | %s to %s", result["rows"], result["start"], result["end"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
