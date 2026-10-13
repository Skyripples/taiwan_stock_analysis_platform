"""Shared Coinbase 5-minute candle validation, storage, planning and metrics."""

from __future__ import annotations

import json
import math
import os
import statistics
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

UTC = timezone.utc
FIVE_MINUTES = 300
SOURCE = "Coinbase Exchange"
PRODUCTS = {"BTC-USD": "BTCUSD", "ETH-USD": "ETHUSD"}


def utc_datetime(value: str | datetime) -> datetime:
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Crypto timestamp must include a timezone")
    return parsed.astimezone(UTC)


def completed_candle_end(now: datetime | None = None) -> datetime:
    current = utc_datetime(now or datetime.now(UTC))
    epoch = int(current.timestamp())
    return datetime.fromtimestamp(epoch - epoch % FIVE_MINUTES, UTC)


def validate_candle(row: dict[str, Any]) -> dict[str, Any]:
    timestamp = utc_datetime(row["timestamp_utc"])
    if int(timestamp.timestamp()) % FIVE_MINUTES:
        raise ValueError("5-minute candle timestamp is not aligned")
    values = {key: float(row[key]) for key in ("open", "high", "low", "close", "volume")}
    if not all(math.isfinite(value) for value in values.values()):
        raise ValueError("OHLCV contains a non-finite value")
    if values["volume"] < 0 or values["low"] > min(values["open"], values["close"], values["high"]) or values["high"] < max(values["open"], values["close"], values["low"]):
        raise ValueError("Invalid OHLCV relationship")
    return {**row, "timestamp_utc": timestamp.isoformat(), **values}


def normalize_rows(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    unique: dict[tuple[str, str], dict[str, Any]] = {}
    for item in rows:
        row = validate_candle(item)
        unique[(str(row["symbol"]), row["timestamp_utc"])] = row
    return sorted(unique.values(), key=lambda row: (row["symbol"], row["timestamp_utc"]))


def completed_rows(rows: Iterable[dict[str, Any]], end: datetime) -> list[dict[str, Any]]:
    """Validate rows and exclude the candle whose interval has not closed yet."""
    boundary = utc_datetime(end)
    return [validate_candle(row) for row in rows if utc_datetime(row["timestamp_utc"]) < boundary]


def missing_candle_windows(start: datetime, end: datetime, existing: set[datetime], limit: int = 300) -> list[tuple[datetime, datetime]]:
    start, end = utc_datetime(start), utc_datetime(end)
    windows: list[tuple[datetime, datetime]] = []
    cursor = start
    while cursor < end:
        if cursor in existing:
            cursor += timedelta(seconds=FIVE_MINUTES)
            continue
        window_start = cursor
        count = 0
        while cursor < end and cursor not in existing and count < limit:
            cursor += timedelta(seconds=FIVE_MINUTES)
            count += 1
        windows.append((window_start, cursor))
    return windows


def read_parquet(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    import pyarrow.parquet as pq
    return pq.ParquetFile(path).read().to_pylist()


def write_parquet_atomic(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    import pyarrow as pa
    import pyarrow.parquet as pq
    normalized = normalize_rows(rows)
    serializable = [
        {key: json.dumps(value, ensure_ascii=False, sort_keys=True) if isinstance(value, (dict, list)) else value for key, value in row.items()}
        for row in normalized
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    os.close(descriptor)
    try:
        pq.write_table(pa.Table.from_pylist(serializable), temporary, compression="zstd", compression_level=6)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def derived_metrics(rows: Iterable[dict[str, Any]]) -> dict[str, float | None]:
    ordered = normalize_rows(rows)
    if not ordered:
        return {key: None for key in ("return_5m", "return_1h", "return_4h", "return_24h", "volatility_7d", "volatility_30d", "ma20", "ma60", "drawdown", "volume_zscore")}
    closes = [row["close"] for row in ordered]
    volumes = [row["volume"] for row in ordered]
    timestamps = [utc_datetime(row["timestamp_utc"]) for row in ordered]
    by_time = dict(zip(timestamps, closes))
    latest_time, latest = timestamps[-1], closes[-1]

    def period_return(seconds: int) -> float | None:
        prior = by_time.get(latest_time - timedelta(seconds=seconds))
        return None if prior in (None, 0) else (latest / prior - 1) * 100

    def volatility(candles: int) -> float | None:
        if len(closes) < candles + 1:
            return None
        values = [math.log(closes[index] / closes[index - 1]) for index in range(len(closes) - candles, len(closes)) if closes[index - 1] > 0 and closes[index] > 0]
        return statistics.pstdev(values) * 100 if len(values) == candles else None

    def average(count: int) -> float | None:
        return sum(closes[-count:]) / count if len(closes) >= count else None

    volume_zscore = None
    if len(volumes) >= 20:
        sample = volumes[-20:]
        deviation = statistics.pstdev(sample)
        volume_zscore = (sample[-1] - statistics.mean(sample)) / deviation if deviation else 0.0
    peak = max(closes)
    return {
        "return_5m": period_return(300), "return_1h": period_return(3600),
        "return_4h": period_return(14400), "return_24h": period_return(86400),
        "volatility_7d": volatility(7 * 24 * 12), "volatility_30d": volatility(30 * 24 * 12),
        "ma20": average(20), "ma60": average(60),
        "drawdown": (latest / peak - 1) * 100 if peak else None, "volume_zscore": volume_zscore,
    }
