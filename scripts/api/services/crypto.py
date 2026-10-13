"""Read-only Coinbase crypto market queries backed by market_intraday_prices."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from api.db import pool

UTC = timezone.utc
CRYPTO_INTERVAL_SECONDS = 300
CRYPTO_STALE_AFTER = timedelta(minutes=10)
CRYPTO_SYMBOLS = ("BTC-USD", "ETH-USD")
CRYPTO_RANGES = {
    "1H": timedelta(hours=1), "4H": timedelta(hours=4),
    "24H": timedelta(hours=24), "7D": timedelta(days=7),
    "30D": timedelta(days=30),
}


def _value(item: Any) -> Any:
    if isinstance(item, Decimal):
        return int(item) if item == item.to_integral() else float(item)
    if isinstance(item, datetime):
        return item.isoformat()
    return item


def _number(value: Any) -> float | None:
    return None if value is None else float(value)


def summarize_candles(symbol: str, rows: list[dict[str, Any]], now: datetime | None = None) -> dict[str, Any]:
    ordered = sorted(rows, key=lambda row: row["timestamp_utc"])
    if not ordered:
        return {"symbol": symbol, "close": None, "change_1h": None, "change_4h": None,
                "change_24h": None, "high_24h": None, "low_24h": None, "volume_24h": None,
                "source": "Coinbase Exchange", "source_date": None, "latest_timestamp": None,
                "fetched_at": None, "stale": None}
    latest_row = ordered[-1]
    latest_at = latest_row["timestamp_utc"].astimezone(UTC)
    latest_close = _number(latest_row["close"])

    def change(hours: int) -> float | None:
        target = latest_at - timedelta(hours=hours)
        candidates = [row for row in ordered if row["timestamp_utc"].astimezone(UTC) <= target and row.get("close") is not None]
        prior = _number(candidates[-1]["close"]) if candidates else None
        return None if latest_close is None or prior in (None, 0) else (latest_close / prior - 1) * 100

    trailing = [row for row in ordered if row["timestamp_utc"].astimezone(UTC) > latest_at - timedelta(hours=24)]
    highs = [_number(row.get("high")) for row in trailing if row.get("high") is not None]
    lows = [_number(row.get("low")) for row in trailing if row.get("low") is not None]
    volumes = [_number(row.get("volume")) for row in trailing if row.get("volume") is not None]
    current = (now or datetime.now(UTC)).astimezone(UTC)
    completed_at = latest_at + timedelta(seconds=CRYPTO_INTERVAL_SECONDS)
    return {
        "symbol": symbol, "close": latest_close, "change_1h": change(1), "change_4h": change(4),
        "change_24h": change(24), "high_24h": max(highs) if highs else None,
        "low_24h": min(lows) if lows else None, "volume_24h": sum(volumes) if volumes else None,
        "source": latest_row.get("source") or "Coinbase Exchange",
        "source_date": latest_at.date().isoformat(), "latest_timestamp": latest_at.isoformat(),
        "fetched_at": _value(latest_row.get("fetched_at")),
        "stale": current - completed_at > CRYPTO_STALE_AFTER,
    }


def get_crypto_summary(now: datetime | None = None) -> dict[str, Any]:
    query = """
        WITH selected AS (
            SELECT mi.instrument_id,mi.source_symbol,ds.name AS source
            FROM market_instruments mi JOIN data_sources ds USING(source_id)
            WHERE mi.source_symbol = ANY(%s)
        ), latest AS (
            SELECT s.instrument_id,max(p.timestamp_utc) AS latest_at
            FROM selected s LEFT JOIN market_intraday_prices p
              ON p.instrument_id=s.instrument_id AND p.interval_seconds=%s
            GROUP BY s.instrument_id
        )
        SELECT s.source_symbol AS symbol,p.timestamp_utc,p.open,p.high,p.low,p.close,p.volume,
               s.source,p.updated_at AS fetched_at
        FROM selected s JOIN latest l USING(instrument_id)
        JOIN market_intraday_prices p ON p.instrument_id=s.instrument_id AND p.interval_seconds=%s
          AND p.timestamp_utc BETWEEN l.latest_at - interval '24 hours' AND l.latest_at
        ORDER BY s.source_symbol,p.timestamp_utc
    """
    with pool.connection() as connection, connection.cursor() as cursor:
        cursor.execute(query, (list(CRYPTO_SYMBOLS), CRYPTO_INTERVAL_SECONDS, CRYPTO_INTERVAL_SECONDS))
        fetched = cursor.fetchall()
    grouped = {symbol: [] for symbol in CRYPTO_SYMBOLS}
    for row in fetched:
        grouped.setdefault(row["symbol"], []).append(row)
    return {"interval_seconds": CRYPTO_INTERVAL_SECONDS, "stale_after_seconds": int(CRYPTO_STALE_AFTER.total_seconds()),
            "results": [summarize_candles(symbol, grouped[symbol], now) for symbol in CRYPTO_SYMBOLS]}


def get_crypto_candles(symbol: str, range_key: str) -> dict[str, Any]:
    query = """
        WITH selected AS (
            SELECT instrument_id FROM market_instruments WHERE source_symbol=%s
        ), latest AS (
            SELECT max(p.timestamp_utc) AS latest_at
            FROM selected s JOIN market_intraday_prices p USING(instrument_id)
            WHERE p.interval_seconds=%s
        )
        SELECT p.timestamp_utc,p.open,p.high,p.low,p.close,p.volume
        FROM selected s JOIN market_intraday_prices p USING(instrument_id) CROSS JOIN latest l
        WHERE p.interval_seconds=%s AND p.timestamp_utc > l.latest_at-%s AND p.timestamp_utc<=l.latest_at
        ORDER BY p.timestamp_utc
    """
    with pool.connection() as connection, connection.cursor() as cursor:
        cursor.execute(query, (symbol, CRYPTO_INTERVAL_SECONDS, CRYPTO_INTERVAL_SECONDS, CRYPTO_RANGES[range_key]))
        rows = cursor.fetchall()
    candles = [{"timestamp": _value(row["timestamp_utc"]), "open": _value(row["open"]),
                "high": _value(row["high"]), "low": _value(row["low"]),
                "close": _value(row["close"]), "volume": _value(row["volume"])} for row in rows]
    return {"symbol": symbol, "range": range_key, "interval_seconds": CRYPTO_INTERVAL_SECONDS,
            "count": len(candles), "candles": candles}
