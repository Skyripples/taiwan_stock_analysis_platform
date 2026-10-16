"""Deterministic TAIFEX tick-to-5-minute reconstruction and persistence helpers."""
from __future__ import annotations

import os
import tempfile
from datetime import date, datetime, time, timezone
from pathlib import Path
from typing import Iterable

from futures_intraday import FuturesCandle, FuturesTick, INTERVAL_SECONDS, TAIPEI, bucket_start
from trading_calendar import get_trading_days_in_range

SOURCE = "TAIFEX official daily tick CSV"
REGULAR_START, REGULAR_END = time(8, 45), time(13, 45, 59)
AFTER_START, AFTER_END = time(15, 0), time(5, 0, 59)


def contract_expiry(contract: str) -> date:
    month = contract[-6:]; year, number = int(month[:4]), int(month[4:])
    first = date(year, number, 1)
    first_wednesday = 1 + (2 - first.weekday()) % 7
    third_wednesday = date(year, number, first_wednesday + 14)
    sessions, _ = get_trading_days_in_range(first, third_wednesday)
    if not sessions: raise ValueError(f"calendar cannot resolve expiry for {contract}")
    return sessions[-1]


def session_for_file_tick(tick: FuturesTick, trading_date: date) -> str:
    local = tick.timestamp.astimezone(TAIPEI); wall = local.time().replace(tzinfo=None)
    if local.date() == trading_date and REGULAR_START <= wall <= REGULAR_END:
        return "regular"
    if (local.date() < trading_date and wall >= AFTER_START) or (local.date() == trading_date and wall <= AFTER_END):
        return "after_hours"
    raise ValueError(f"tick timestamp is inconsistent with TAIFEX trading date {trading_date}")


def aggregate_ticks(ticks: Iterable[FuturesTick], trading_date: date, *,
                    available_at: datetime, fetched_at: datetime | None = None) -> list[FuturesCandle]:
    fetched = (fetched_at or datetime.now(timezone.utc)).astimezone(timezone.utc)
    buckets: dict[tuple, dict] = {}
    for sequence, tick in enumerate(ticks):
        session = session_for_file_tick(tick, trading_date); stamp = bucket_start(tick.timestamp)
        key = (tick.product, tick.contract, session, stamp); item = buckets.get(key)
        if tick.volume is None: raise ValueError("TAIFEX tick volume is unavailable")
        if item is None:
            buckets[key] = {"open": tick.price, "high": tick.price, "low": tick.price,
                            "close": tick.price, "volume": tick.volume, "sequence": sequence}
        else:
            item["high"] = max(item["high"], tick.price); item["low"] = min(item["low"], tick.price)
            item["close"] = tick.price; item["volume"] += tick.volume; item["sequence"] = sequence
    output = []
    for (product, contract, session, stamp), item in sorted(buckets.items(), key=lambda pair: pair[0]):
        candle = FuturesCandle(stamp, trading_date, product, contract, session, INTERVAL_SECONDS,
                               item["open"], item["high"], item["low"], item["close"], item["volume"],
                               SOURCE, available_at.astimezone(timezone.utc), fetched)
        try:
            candle.days_to_expiry = (contract_expiry(contract) - trading_date).days
        except ValueError:
            # Far-dated contracts may exceed the PIT calendar coverage. Preserve
            # the official candle and NULL instead of guessing an expiry date.
            candle.days_to_expiry = None
        candle.validate(); output.append(candle)
    return output


def parquet_path(root: Path, trading_date: date, product: str) -> Path:
    return root / f"trading_date={trading_date.isoformat()}" / f"product={product}" / "candles.parquet"


def write_parquet_atomic(path: Path, candles: Iterable[FuturesCandle]) -> None:
    import pyarrow as pa
    import pyarrow.parquet as pq
    rows = [candle.row() for candle in candles]; path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent); os.close(descriptor)
    try:
        pq.write_table(pa.Table.from_pylist(rows), temporary, compression="zstd", compression_level=6)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary): os.unlink(temporary)


UPSERT = """INSERT INTO futures_intraday_prices
 (timestamp_utc,trading_date,interval_seconds,product,contract,session,open,high,low,close,
  volume,days_to_expiry,source,available_at,fetched_at)
 VALUES (%(timestamp_utc)s,%(trading_date)s,%(interval_seconds)s,%(product)s,%(contract)s,
         %(session)s,%(open)s,%(high)s,%(low)s,%(close)s,%(volume)s,%(days_to_expiry)s,
         %(source)s,%(available_at)s,%(fetched_at)s)
 ON CONFLICT (product,contract,session,timestamp_utc,interval_seconds) DO UPDATE SET
 trading_date=EXCLUDED.trading_date,open=EXCLUDED.open,high=EXCLUDED.high,low=EXCLUDED.low,
 close=EXCLUDED.close,volume=EXCLUDED.volume,source=EXCLUDED.source,
 available_at=EXCLUDED.available_at,fetched_at=EXCLUDED.fetched_at,updated_at=now()"""


def upsert_candles(connection, candles: Iterable[FuturesCandle]) -> int:
    rows = [{**candle.row(), "days_to_expiry": candle.days_to_expiry} for candle in candles]
    if not rows: return 0
    with connection.transaction(), connection.cursor() as cursor: cursor.executemany(UPSERT, rows)
    return len(rows)
