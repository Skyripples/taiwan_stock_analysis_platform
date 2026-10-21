"""Checkpointed global-market historical backfill for PostgreSQL.

This command is intentionally independent from the formal prediction and
Market Score pipelines. Candidate registry entries remain disabled.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import logging
import math
import os
import random
import time
from dataclasses import dataclass
from datetime import date, datetime, time as dt_time, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable
from zoneinfo import ZoneInfo

import requests

from config import PROJECT_ROOT
from database.connection import apply_migrations, configured, connect
from database.global_market_repository import GlobalMarketRepository
from research.global_market_alignment import (
    UTC, commodity_available_at, crypto_available_at, fx_available_at,
    icsa_available_at, target_cutoff, treasury_available_at,
    us_session_available_at,
)


LOGGER = logging.getLogger("global_market_backfill")
REPORT = PROJECT_ROOT / "data" / "analysis" / "current" / "global_market_backfill_report.json"
LAKE_ROOT = PROJECT_ROOT / "data_lake" / "global_market"
LOCAL_CHECKPOINT = LAKE_ROOT / "_checkpoints" / "global_market_backfill.json"
YAHOO = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
VIX_CSV = "https://cdn.cboe.com/api/global/us_indices/daily_prices/VIX_History.csv"
FRED_CSV = "https://fred.stlouisfed.org/graph/fredgraph.csv"
COINBASE = "https://api.exchange.coinbase.com/products/{product}/candles"
MAX_START = date.today() - timedelta(days=365 * 20 + 7)
RESUME_OVERLAP_DAYS = 3


@dataclass(frozen=True)
class Instrument:
    symbol: str
    source_symbol: str
    name: str
    category: str
    source: str
    market: str
    country: str
    exchange: str
    currency: str
    timezone: str
    inception: date
    availability: str


INSTRUMENTS = (
    Instrument("SOX", "^SOX", "PHLX Semiconductor Sector Index", "market", "yahoo", "US", "US", "Nasdaq GIDS", "USD", "America/New_York", date(1993, 12, 1), "us"),
    Instrument("TSM_ADR", "TSM", "Taiwan Semiconductor Manufacturing ADR", "market", "yahoo", "US", "US", "NYSE", "USD", "America/New_York", date(1997, 10, 9), "us"),
    Instrument("NASDAQ", "^IXIC", "Nasdaq Composite", "market", "yahoo", "US", "US", "Nasdaq GIDS", "USD", "America/New_York", date(1971, 2, 5), "us"),
    Instrument("SP500", "^GSPC", "S&P 500", "market", "yahoo", "US", "US", "S&P Dow Jones Indices", "USD", "America/New_York", date(1957, 3, 4), "us"),
    Instrument("NDX", "^NDX", "Nasdaq 100", "market", "yahoo", "US", "US", "Nasdaq GIDS", "USD", "America/New_York", date(1985, 1, 31), "us"),
    Instrument("RUT", "^RUT", "Russell 2000", "market", "yahoo", "US", "US", "Russell", "USD", "America/New_York", date(1987, 9, 10), "us"),
    Instrument("EWT", "EWT", "iShares MSCI Taiwan ETF", "market", "yahoo", "US", "US", "NYSE Arca", "USD", "America/New_York", date(2000, 6, 20), "us"),
    Instrument("VIX", "VIX", "Cboe Volatility Index", "volatility", "cboe", "US", "US", "Cboe", "index points", "America/Chicago", date(1990, 1, 2), "us"),
    Instrument("COPPER", "HG=F", "Copper Futures", "commodity", "yahoo", "Global", "US", "COMEX", "USD", "America/New_York", date(1988, 1, 1), "commodity"),
    Instrument("WTI", "CL=F", "WTI Crude Oil Futures", "commodity", "yahoo", "Global", "US", "NYMEX", "USD", "America/New_York", date(1983, 3, 30), "commodity"),
    Instrument("GOLD", "GC=F", "Gold Futures", "commodity", "yahoo", "Global", "US", "COMEX", "USD", "America/New_York", date(1974, 12, 31), "commodity"),
    Instrument("USDJPY", "JPY=X", "USD/JPY", "fx", "yahoo", "OTC FX", "Global", "OTC", "JPY per USD", "America/New_York", date(1971, 1, 1), "fx"),
    Instrument("USDKRW", "KRW=X", "USD/KRW", "fx", "yahoo", "OTC FX", "Global", "OTC", "KRW per USD", "America/New_York", date(1981, 1, 1), "fx"),
    Instrument("USDCNH", "CNH=X", "USD/CNH", "fx", "yahoo", "OTC FX", "Global", "OTC", "CNH per USD", "America/New_York", date(2010, 8, 23), "fx"),
    Instrument("USDTWD", "TWD=X", "USD/TWD", "fx", "yahoo", "OTC FX", "Global", "OTC", "TWD per USD", "America/New_York", date(1983, 10, 3), "fx"),
    Instrument("BTCUSD", "BTC-USD", "Bitcoin / US Dollar", "crypto", "coinbase", "Crypto", "Global", "Coinbase", "USD", "UTC", date(2015, 7, 20), "crypto"),
    Instrument("ETHUSD", "ETH-USD", "Ethereum / US Dollar", "crypto", "coinbase", "Crypto", "Global", "Coinbase", "USD", "UTC", date(2016, 5, 18), "crypto"),
)
SERIES = {
    "DGS5": ("US Treasury 5-Year", "rates", "Percent", date(1962, 1, 2)),
    "DGS10": ("US Treasury 10-Year", "rates", "Percent", date(1962, 1, 2)),
    "DGS30": ("US Treasury 30-Year", "rates", "Percent", date(1977, 2, 15)),
    "T10Y3M": ("10-Year minus 3-Month Treasury Spread", "rates", "Percentage points", date(1982, 1, 4)),
    "ICSA": ("Initial Jobless Claims", "macro", "Number", date(1967, 1, 7)),
}
SOURCES = {
    "yahoo": {"name": "Yahoo Finance", "official": False, "url": "https://finance.yahoo.com/", "transport": "JSON", "license": "Research quote source; Yahoo terms apply.", "rate": "Throttled to <=2 requests/second."},
    "cboe": {"name": "Cboe Global Markets", "official": True, "url": VIX_CSV, "transport": "CSV", "license": "Cboe attribution/redistribution terms apply.", "rate": "One full-file request."},
    "fred": {"name": "Federal Reserve Bank of St. Louis FRED", "official": True, "url": FRED_CSV, "transport": "CSV", "license": "FRED and originating agency terms apply.", "rate": "Sequential cached requests."},
    "coinbase": {"name": "Coinbase Exchange", "official": True, "url": "https://api.exchange.coinbase.com/", "transport": "JSON", "license": "Coinbase Market Data Terms apply.", "rate": "Public limit 10 req/s; client uses <=4 req/s."},
}


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Backfill normalized global market history")
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--category", choices=("market", "volatility", "commodity", "fx", "rates", "crypto", "macro"))
    parser.add_argument("--symbol", action="append", help="canonical symbol/series; repeatable")
    parser.add_argument("--start", type=date.fromisoformat)
    parser.add_argument("--end", type=lambda x: date.today() if x == "today" else date.fromisoformat(x), default=date.today())
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--storage", choices=("local", "postgres"), default="local",
                        help="local writes canonical Parquet; postgres is an explicit database backfill")
    parser.add_argument("--init-schema", action="store_true")
    args = parser.parse_args()
    if not (args.all or args.category or args.symbol or args.init_schema):
        parser.error("choose --all, --category, --symbol, or --init-schema")
    if args.storage == "local" and args.init_schema:
        parser.error("--init-schema is only valid with --storage postgres")
    return args


class Client:
    def __init__(self) -> None:
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "taiwan-stock-analysis-platform/3.13", "Accept": "*/*"})
        self.last_request = 0.0

    def get(self, url: str, *, min_interval: float = 0.25, **kwargs: Any) -> requests.Response:
        error: Exception | None = None
        for attempt in range(4):
            wait = min_interval - (time.monotonic() - self.last_request)
            if wait > 0:
                time.sleep(wait)
            try:
                response = self.session.get(url, timeout=(10, 60), **kwargs)
                self.last_request = time.monotonic()
                if response.status_code == 429:
                    time.sleep(float(response.headers.get("Retry-After") or 2 ** (attempt + 1)))
                    continue
                response.raise_for_status()
                return response
            except requests.RequestException as exc:
                error = exc
                if attempt < 3:
                    time.sleep(2 ** attempt + random.random())
        raise RuntimeError(f"Request failed after retries: {url}: {error}")


def _number(value: Any) -> float | None:
    if value in (None, "", "."):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def _sanitize_ohlc(values: dict[str, float | None]) -> tuple[dict[str, float | None], dict[str, Any]]:
    """Preserve a valid close while nulling internally inconsistent source OHLC.

    Some Yahoo continuous-futures/FX history and a few legacy Cboe rows expose
    mutually inconsistent O/H/L/C fields. They must not be silently repaired.
    The raw values remain in metadata and the unusable O/H/L fields become null.
    """

    opening, high, low, close = (values.get(key) for key in ("open", "high", "low", "close"))
    invalid = bool(
        high is not None and low is not None and (
            high < low
            or (opening is not None and not low <= opening <= high)
            or (close is not None and not low <= close <= high)
        )
    )
    if not invalid:
        return values, {}
    raw = dict(values)
    return {**values, "open": None, "high": None, "low": None}, {
        "source_ohlc_inconsistent": True, "raw_ohlc": raw,
    }


def fetch_yahoo(
    client: Client, instrument: Instrument, start: date, end: date,
    skipped: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    response = client.get(YAHOO.format(symbol=requests.utils.quote(instrument.source_symbol, safe="")), params={
        "period1": int(datetime.combine(start, dt_time(), UTC).timestamp()),
        "period2": int(datetime.combine(end + timedelta(days=1), dt_time(), UTC).timestamp()),
        "interval": "1d", "events": "history",
    }, min_interval=0.55)
    fetched_at = datetime.now(UTC)
    payload = response.json()["chart"]["result"][0]
    quote = payload["indicators"]["quote"][0]
    adjusted = (payload["indicators"].get("adjclose") or [{}])[0].get("adjclose") or []
    zone = ZoneInfo(payload.get("meta", {}).get("exchangeTimezoneName") or instrument.timezone)
    output = []
    for index, timestamp in enumerate(payload.get("timestamp") or []):
        values = {key: _number((quote.get(key) or [None] * (index + 1))[index]) for key in ("open", "high", "low", "close", "volume")}
        if values["close"] is None:
            continue
        trade_date = datetime.fromtimestamp(timestamp, zone).date()
        if not start <= trade_date <= end:
            continue
        if instrument.availability == "us":
            available = us_session_available_at(trade_date)
        elif instrument.availability == "commodity":
            available = commodity_available_at(trade_date, instrument.source_symbol)
        else:
            available = fx_available_at(trade_date)
        if fetched_at < available.astimezone(UTC):
            if skipped is not None:
                skipped.append({"trade_date": trade_date.isoformat(), "status": "current_session",
                    "reason": "not_closed", "fetched_at": _iso(fetched_at), "available_at": _iso(available)})
            continue
        values, flags = _sanitize_ohlc(values)
        output.append({"trade_date": trade_date.isoformat(), **values,
            "adjusted_close": _number(adjusted[index]) if index < len(adjusted) else values["close"],
            "available_at": _iso(available), "source_updated_at": None,
            "quality_flags": flags, "metadata": {"source": instrument.source, "source_symbol": instrument.source_symbol,
                "availability_rule": instrument.availability, "fetched_at": _iso(fetched_at), **flags}})
    return output


def fetch_vix(
    client: Client, start: date, end: date, skipped: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    response = client.get(VIX_CSV, min_interval=0)
    fetched_at = datetime.now(UTC)
    rows = csv.DictReader(io.StringIO(response.text))
    output = []
    for row in rows:
        trade_date = datetime.strptime(row["DATE"], "%m/%d/%Y").date()
        if not start <= trade_date <= end:
            continue
        available = us_session_available_at(trade_date)
        if fetched_at < available.astimezone(UTC):
            if skipped is not None:
                skipped.append({"trade_date": trade_date.isoformat(), "status": "current_session",
                    "reason": "not_closed", "fetched_at": _iso(fetched_at), "available_at": _iso(available)})
            continue
        values = {key.lower(): _number(row[key]) for key in ("OPEN", "HIGH", "LOW", "CLOSE")}
        values, flags = _sanitize_ohlc(values)
        output.append({"trade_date": trade_date.isoformat(), **values, "adjusted_close": values["close"], "volume": None,
            "available_at": _iso(available), "source_updated_at": None,
            "quality_flags": flags, "metadata": {"source": "cboe", "official_file": VIX_CSV,
                "fetched_at": _iso(fetched_at), **flags}})
    return output


def fetch_fred(client: Client, series: str, start: date, end: date) -> list[dict[str, Any]]:
    response = client.get(FRED_CSV, params={"id": series, "cosd": start.isoformat(), "coed": end.isoformat()}, min_interval=0.3)
    fetched_at = datetime.now(UTC)
    text = response.text
    output = []
    for row in csv.DictReader(io.StringIO(text)):
        value = _number(row.get(series))
        if value is None:
            continue
        observation = date.fromisoformat(row["observation_date"])
        # fredgraph.csv may return the full series when a requested interval
        # contains no released observation. Enforce the caller's range again.
        if not start <= observation <= end:
            continue
        available = icsa_available_at(observation) if series == "ICSA" else treasury_available_at(observation)
        output.append({"observation_date": observation.isoformat(), "available_at": _iso(available), "value": value,
            "value_text": None, "unit": SERIES[series][2], "vintage_date": available.date().isoformat(),
            "preliminary": series == "ICSA", "source_updated_at": None,
            "metadata": {"source": "fred", "source_series_id": series, "fetched_at": _iso(fetched_at),
                "availability_rule": "Thursday 08:30 ET release" if series == "ICSA" else "conservative 16:00 ET same-day publication"}})
    return output


def crypto_windows(start: datetime, end: datetime, hours: int = 300) -> Iterable[tuple[datetime, datetime]]:
    cursor = start
    while cursor < end:
        stop = min(cursor + timedelta(hours=hours), end)
        yield cursor, stop
        cursor = stop


def fetch_crypto_chunk(client: Client, product: str, start: datetime, end: datetime, granularity: int = 3600) -> list[dict[str, Any]]:
    if granularity not in (60, 300, 900, 3600, 21600, 86400):
        raise ValueError(f"Unsupported Coinbase candle granularity: {granularity}")
    response = client.get(COINBASE.format(product=product), params={
        "start": start.isoformat().replace("+00:00", "Z"), "end": end.isoformat().replace("+00:00", "Z"), "granularity": granularity,
    }, min_interval=0.27)
    output = []
    for raw in response.json():
        timestamp = datetime.fromtimestamp(int(raw[0]), UTC)
        if not start <= timestamp < end:
            continue
        low, high, opening, close, volume = map(_number, raw[1:6])
        output.append({"timestamp_utc": timestamp.isoformat(), "interval_seconds": granularity,
            "open": opening, "high": high, "low": low, "close": close, "volume": volume,
            "timezone": "UTC", "available_at": _iso(crypto_available_at(timestamp, granularity)),
            "quality_flags": {}, "metadata": {"product": product}})
    return sorted(output, key=lambda row: row["timestamp_utc"])


def _quality(rows: list[dict[str, Any]], key: str, source: str, duration: float) -> dict[str, Any]:
    values = [row[key] for row in rows]
    duplicates = len(values) - len(set(values))
    numeric_fields = ("open", "high", "low", "close") if "open" in (rows[0] if rows else {}) else ("value",)
    possible = len(rows) * len(numeric_fields)
    missing = sum(row.get(field) is None for row in rows for field in numeric_fields)
    return {"first_date": min(values) if values else None, "last_date": max(values) if values else None,
        "rows": len(rows), "missing_rate": round(missing / possible, 8) if possible else 0,
        "duplicate_count": duplicates, "stale_days": (date.today() - date.fromisoformat(max(values)[:10])).days if values else None,
        "source": source, "fetch_duration_seconds": round(duration, 3)}


def _atomic_report(payload: dict[str, Any]) -> None:
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    temporary = REPORT.with_name(f".{REPORT.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, REPORT)
    finally:
        temporary.unlink(missing_ok=True)


def _utc_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError(f"Timestamp must be timezone-aware: {value}")
    return parsed.astimezone(UTC)


def _json_value(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value or {}, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _metadata(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value:
        parsed = json.loads(value)
        return parsed if isinstance(parsed, dict) else {}
    return {}


def daily_row_is_closed(row: dict[str, Any]) -> bool:
    """Only completed daily candles are eligible for canonical ingestion."""

    metadata = _metadata(row.get("metadata"))
    fetched = row.get("fetched_at") or metadata.get("fetched_at") or row.get("created_at")
    available = row.get("available_at")
    if fetched is None or available is None:
        return False
    return _utc_datetime(fetched) >= _utc_datetime(available)


def resume_start(base_start: date, checkpoint: dict[str, Any] | None, *, local: bool) -> date:
    if not checkpoint or not checkpoint.get("last_cursor"):
        return base_start
    cursor = date.fromisoformat(str(checkpoint["last_cursor"])[:10])
    if local:
        cursor -= timedelta(days=RESUME_OVERLAP_DAYS)
    return max(base_start, cursor)


def _parquet_rows(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    import pyarrow.parquet as pq
    return pq.ParquetFile(path).read().to_pylist()


def _write_parquet_atomic(path: Path, rows: list[dict[str, Any]], schema: Any, dictionary: list[str]) -> None:
    import pyarrow as pa
    import pyarrow.parquet as pq

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        table = pa.Table.from_pylist(rows, schema=schema)
        pq.write_table(table, temporary, compression="zstd", compression_level=6,
                       use_dictionary=[name for name in dictionary if name in table.column_names])
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _daily_schema() -> Any:
    import pyarrow as pa
    timestamp = pa.timestamp("us", tz="UTC")
    return pa.schema([
        ("category", pa.string()), ("partition_year", pa.int64()), ("canonical_symbol", pa.string()),
        ("instrument_id", pa.int64()), ("trade_date", pa.date32()),
        ("open", pa.float64()), ("high", pa.float64()), ("low", pa.float64()),
        ("close", pa.float64()), ("adjusted_close", pa.float64()), ("volume", pa.float64()),
        ("currency", pa.string()), ("session", pa.string()), ("timezone", pa.string()),
        ("available_at", timestamp), ("source_updated_at", timestamp),
        ("source", pa.string()), ("fetched_at", timestamp),
        ("quality_flags", pa.string()), ("metadata", pa.string()),
        ("created_at", timestamp), ("updated_at", timestamp),
    ])


def _macro_schema() -> Any:
    import pyarrow as pa
    timestamp = pa.timestamp("us", tz="UTC")
    return pa.schema([
        ("series_key", pa.string()), ("partition_year", pa.int64()), ("macro_series_id", pa.int64()),
        ("observation_date", pa.date32()), ("available_at", timestamp), ("value", pa.float64()),
        ("value_text", pa.string()), ("unit", pa.string()), ("vintage_date", pa.date32()),
        ("preliminary", pa.bool_()), ("source_updated_at", timestamp),
        ("source", pa.string()), ("fetched_at", timestamp), ("metadata", pa.string()),
        ("created_at", timestamp), ("updated_at", timestamp),
    ])


def _existing_local_row(row: dict[str, Any]) -> dict[str, Any]:
    output = dict(row)
    metadata = _metadata(output.get("metadata"))
    fetched = output.get("fetched_at") or metadata.get("fetched_at") or output.get("created_at")
    output["source"] = output.get("source") or metadata.get("source")
    output["fetched_at"] = _utc_datetime(fetched) if fetched else None
    output["metadata"] = _json_value(metadata)
    if "quality_flags" in output:
        output["quality_flags"] = _json_value(output.get("quality_flags"))
    return output


def _daily_key(row: dict[str, Any]) -> tuple[str, date]:
    return str(row["canonical_symbol"]), date.fromisoformat(str(row["trade_date"])[:10])


def write_local_daily(
    instrument: Instrument, rows: list[dict[str, Any]], root: Path | None = None,
    cleanup_start: date | None = None, cleanup_end: date | None = None,
) -> dict[str, Any]:
    root = root or LAKE_ROOT
    groups: dict[int, list[dict[str, Any]]] = {}
    skipped: list[dict[str, Any]] = []
    for source in rows:
        trade_date = date.fromisoformat(str(source["trade_date"])[:10])
        metadata = _metadata(source.get("metadata"))
        fetched = _utc_datetime(metadata["fetched_at"])
        candidate = {
            "category": instrument.category, "partition_year": trade_date.year,
            "canonical_symbol": instrument.symbol, "instrument_id": None, "trade_date": trade_date,
            "open": source.get("open"), "high": source.get("high"), "low": source.get("low"),
            "close": source.get("close"), "adjusted_close": source.get("adjusted_close"), "volume": source.get("volume"),
            "currency": instrument.currency, "session": "regular", "timezone": instrument.timezone,
            "available_at": _utc_datetime(source["available_at"]),
            "source_updated_at": _utc_datetime(source.get("source_updated_at")),
            "source": instrument.source, "fetched_at": fetched,
            "quality_flags": _json_value(source.get("quality_flags")), "metadata": _json_value(metadata),
            "created_at": fetched, "updated_at": fetched,
        }
        if not daily_row_is_closed(candidate):
            skipped.append({"trade_date": trade_date.isoformat(), "status": "current_session",
                "reason": "not_closed", "fetched_at": fetched.isoformat(),
                "available_at": candidate["available_at"].isoformat()})
            continue
        groups.setdefault(trade_date.year, []).append(candidate)
    years = set(groups)
    if cleanup_start and cleanup_end:
        years.update(range(cleanup_start.year, cleanup_end.year + 1))
    files = 0
    removed = 0
    for year in sorted(years):
        new_rows = groups.get(year, [])
        path = root / "daily_prices" / f"category={instrument.category}" / f"partition_year={year}" / "part-00000.parquet"
        existing = [_existing_local_row(row) for row in _parquet_rows(path)]
        kept = []
        for row in existing:
            row_date = date.fromisoformat(str(row["trade_date"])[:10])
            in_cleanup = (row.get("canonical_symbol") == instrument.symbol and cleanup_start is not None
                          and cleanup_end is not None and cleanup_start <= row_date <= cleanup_end)
            if in_cleanup and not daily_row_is_closed(row):
                removed += 1
                continue
            kept.append(row)
        if not new_rows and len(kept) == len(existing):
            continue
        merged = {_daily_key(row): row for row in kept}
        merged.update({_daily_key(row): row for row in new_rows})
        ordered = sorted(merged.values(), key=lambda row: (row["canonical_symbol"], row["trade_date"]))
        _write_parquet_atomic(path, ordered, _daily_schema(), ["category", "canonical_symbol", "source"])
        files += 1
    return {"rows": sum(len(values) for values in groups.values()), "files": files,
            "skipped": skipped, "removed_ineligible": removed}


def _macro_key(row: dict[str, Any]) -> tuple[str, date, datetime]:
    return (str(row["series_key"]), date.fromisoformat(str(row["observation_date"])[:10]),
            _utc_datetime(row["available_at"]))


def write_local_macro(series: str, rows: list[dict[str, Any]], root: Path | None = None) -> dict[str, Any]:
    root = root or LAKE_ROOT
    groups: dict[int, list[dict[str, Any]]] = {}
    for source in rows:
        observation = date.fromisoformat(str(source["observation_date"])[:10])
        metadata = _metadata(source.get("metadata"))
        fetched = _utc_datetime(metadata["fetched_at"])
        groups.setdefault(observation.year, []).append({
            "series_key": series, "partition_year": observation.year, "macro_series_id": None,
            "observation_date": observation, "available_at": _utc_datetime(source["available_at"]),
            "value": source.get("value"), "value_text": source.get("value_text"), "unit": source.get("unit"),
            "vintage_date": date.fromisoformat(str(source["vintage_date"])[:10]) if source.get("vintage_date") else None,
            "preliminary": source.get("preliminary"), "source_updated_at": _utc_datetime(source.get("source_updated_at")),
            "source": "fred", "fetched_at": fetched, "metadata": _json_value(metadata),
            "created_at": fetched, "updated_at": fetched,
        })
    files = 0
    for year, new_rows in sorted(groups.items()):
        path = root / "macro_observations" / f"series_key={series}" / f"partition_year={year}" / "part-00000.parquet"
        merged = {_macro_key(row): _existing_local_row(row) for row in _parquet_rows(path)}
        merged.update({_macro_key(row): row for row in new_rows})
        ordered = sorted(merged.values(), key=lambda row: (row["observation_date"], row["available_at"]))
        _write_parquet_atomic(path, ordered, _macro_schema(), ["series_key", "source"])
        files += 1
    return {"rows": len(rows), "files": files}


def load_local_checkpoints(path: Path | None = None) -> dict[str, Any]:
    path = path or LOCAL_CHECKPOINT
    if not path.exists():
        return {"version": "1.0", "tasks": {}}
    payload = json.loads(path.read_text(encoding="utf-8"))
    return payload if isinstance(payload, dict) and isinstance(payload.get("tasks"), dict) else {"version": "1.0", "tasks": {}}


def save_local_checkpoints(payload: dict[str, Any], path: Path | None = None) -> None:
    path = path or LOCAL_CHECKPOINT
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    payload["updated_at"] = datetime.now(UTC).isoformat()
    try:
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def bootstrap(repository: GlobalMarketRepository) -> None:
    repository.upsert_many("data_sources", [{"source_key": key, "name": item["name"], "official": item["official"],
        "base_url": item["url"], "transport": item["transport"], "authentication_type": "none",
        "license_notes": item["license"], "rate_limit_notes": item["rate"], "active": True} for key, item in SOURCES.items()])
    repository.connection.commit()
    sources = repository.source_ids()
    repository.upsert_many("market_instruments", [{"source_id": sources[item.source], "canonical_symbol": item.symbol,
        "source_symbol": item.source_symbol, "name": item.name, "category": item.category, "market": item.market,
        "country": item.country, "exchange": item.exchange, "currency": item.currency, "timezone": item.timezone,
        "native_frequency": "1h" if item.category == "crypto" else "1d", "trading_hours": "24/7" if item.category == "crypto" else "source-specific",
        "adjusted_policy": "unadjusted OHLC; adjusted_close stored when supplied", "inception_date": item.inception.isoformat(),
        "active": True, "metadata": {"availability_rule": item.availability}} for item in INSTRUMENTS])
    repository.upsert_many("macro_series", [{"source_id": sources["fred"], "series_key": key, "source_series_id": key,
        "name": spec[0], "country": "US", "frequency": "weekly" if key == "ICSA" else "daily", "unit": spec[2],
        "seasonal_adjustment": None, "timezone": "America/New_York", "release_rule": "Thursday 08:30 ET" if key == "ICSA" else "conservative same-day 16:00 ET",
        "inception_date": spec[3].isoformat(), "metadata": {"source": "FRED", "point_in_time_policy": "available_at required"}} for key, spec in SERIES.items()])
    repository.connection.commit()


def selected(args: argparse.Namespace) -> tuple[list[Instrument], list[str]]:
    wanted = {value.upper() for value in (args.symbol or [])}
    instruments = [item for item in INSTRUMENTS if (args.all or args.category == item.category or item.symbol in wanted or item.source_symbol.upper() in wanted)]
    series = [key for key, spec in SERIES.items() if (args.all or args.category == spec[1] or key in wanted)]
    unknown = wanted - {item.symbol for item in instruments} - {item.source_symbol.upper() for item in instruments} - set(series)
    if unknown:
        raise ValueError(f"Unknown symbols: {', '.join(sorted(unknown))}")
    return instruments, series


def target_dates() -> list[date]:
    dates: set[date] = set()
    for path, field in ((PROJECT_ROOT / "data/history/backfill_market_daily.csv", "prediction_target_date"), (PROJECT_ROOT / "data/history/market_daily.csv", "trade_date")):
        if not path.exists():
            continue
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                if row.get(field):
                    dates.add(date.fromisoformat(row[field]))
    calendar = PROJECT_ROOT / "data/calendar/twse_trading_calendar.json"
    if calendar.exists():
        payload = json.loads(calendar.read_text(encoding="utf-8"))
        for year in payload.get("years", {}).values():
            for value in year.get("trading_days", []):
                dates.add(date.fromisoformat(value))
    return sorted(dates)


def build_crypto_features(connection, instrument_id: int, symbol: str) -> int:
    windows = (1, 4, 12, 24) if symbol == "BTCUSD" else (24,)
    dates = target_dates()
    if not dates:
        return 0
    with connection.cursor() as cursor:
        cursor.execute("""SELECT timestamp_utc, available_at, close FROM market_intraday_prices
                          WHERE instrument_id=%s AND interval_seconds=3600 ORDER BY available_at""", (instrument_id,))
        rows = cursor.fetchall()
    by_available = {row["available_at"].astimezone(UTC): float(row["close"]) for row in rows if row["close"] is not None}
    keys = sorted(by_available)
    from bisect import bisect_left, bisect_right
    output = []
    for target in dates:
        cutoff = target_cutoff(target).astimezone(UTC)
        current_index = bisect_left(keys, cutoff) - 1
        if current_index < 0:
            continue
        current_at = keys[current_index]
        current = by_available[current_at]
        for hours in windows:
            prior_at = current_at - timedelta(hours=hours)
            prior_index = bisect_right(keys, prior_at) - 1
            if prior_index < 0 or by_available[keys[prior_index]] == 0:
                continue
            output.append({"feature_key": f"{symbol.lower()}_return_{hours}h", "target_date": target.isoformat(),
                "target_scope": "TAIEX", "cutoff_at": cutoff.isoformat(), "value": (current / by_available[keys[prior_index]] - 1) * 100,
                "available_at": current_at.isoformat(), "source_instrument_id": instrument_id, "transform_version": "1.0",
                "quality_flags": {}, "metadata": {"window_hours": hours, "strict_cutoff": True}})
    return GlobalMarketRepository(connection).upsert_many("market_features", output)


def main() -> int:
    args = arguments()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
    started = time.monotonic()
    client = Client()
    connection = None
    repository = None
    if not args.dry_run and args.storage == "postgres":
        if not configured():
            raise RuntimeError("Database environment is required for --storage postgres")
        connection = connect()
        if args.init_schema:
            applied = apply_migrations(connection)
            connection.commit()
            LOGGER.info("Migrations applied: %s", applied or "none")
        repository = GlobalMarketRepository(connection)
        bootstrap(repository)
    if args.init_schema and not (args.all or args.category or args.symbol):
        return 0
    instruments, series = selected(args)
    if args.storage == "local" and any(item.category == "crypto" for item in instruments):
        raise ValueError("Crypto local history uses scripts/backfill_crypto_history.py")
    local_checkpoints = load_local_checkpoints() if args.storage == "local" and args.resume else {"version": "1.0", "tasks": {}}
    report: dict[str, Any] = {"version": "1.1", "generated_at": datetime.now(UTC).isoformat(), "dry_run": args.dry_run,
        "storage": args.storage,
        "requested_start": args.start.isoformat() if args.start else None, "requested_end": args.end.isoformat(),
        "datasets": {}, "failures": [], "table_counts": {}, "total_duration_seconds": None}
    try:
        instrument_ids = repository.instrument_ids() if repository else {}
        series_ids = repository.series_ids() if repository else {}
        for item in instruments:
            start = max(args.start or item.inception, item.inception)
            if item.symbol != "VIX":
                start = max(start, MAX_START)
            task = f"{item.category}:{item.symbol}"
            checkpoint = repository.checkpoint(task) if repository and args.resume else local_checkpoints["tasks"].get(task) if args.storage == "local" and args.resume else None
            start = resume_start(start, checkpoint, local=args.storage == "local")
            tick = time.monotonic()
            try:
                if item.category == "crypto":
                    all_rows: list[dict[str, Any]] = []
                    begin = datetime.combine(start, dt_time(), UTC)
                    finish = datetime.combine(args.end + timedelta(days=1), dt_time(), UTC)
                    for chunk_start, chunk_end in crypto_windows(begin, finish):
                        rows = fetch_crypto_chunk(client, item.source_symbol, chunk_start, chunk_end)
                        all_rows.extend(rows)
                        if repository and rows:
                            iid = instrument_ids[item.symbol]
                            repository.upsert_many("market_intraday_prices", [dict(row, instrument_id=iid) for row in rows])
                            repository.save_checkpoint(task, "running", chunk_end.date().isoformat(), len(all_rows), metadata={"chunk_end": chunk_end.isoformat()})
                            connection.commit()
                    if repository:
                        repository.save_checkpoint(task, "complete", args.end.isoformat(), len(all_rows))
                        feature_rows = build_crypto_features(connection, instrument_ids[item.symbol], item.symbol)
                        connection.commit()
                    else:
                        feature_rows = 0
                    report["datasets"][item.symbol] = {**_quality(all_rows, "timestamp_utc", item.source, time.monotonic()-tick), "feature_rows_upserted": feature_rows}
                else:
                    skipped: list[dict[str, Any]] = []
                    rows = fetch_vix(client, start, args.end, skipped) if item.symbol == "VIX" else fetch_yahoo(client, item, start, args.end, skipped)
                    if repository:
                        iid = instrument_ids[item.symbol]
                        repository.upsert_many("market_daily_prices", [dict(row, instrument_id=iid, currency=item.currency, session="regular", timezone=item.timezone) for row in rows])
                        repository.save_checkpoint(task, "complete", args.end.isoformat(), len(rows))
                        connection.commit()
                        local_stats = {"files": 0, "skipped": [], "removed_ineligible": 0}
                    elif args.storage == "local" and not args.dry_run:
                        local_stats = write_local_daily(item, rows, cleanup_start=start, cleanup_end=args.end)
                        local_checkpoints["tasks"][task] = {"status": "complete", "last_cursor": args.end.isoformat(),
                            "rows_written": local_stats["rows"], "skipped_current_session": len(skipped) + len(local_stats["skipped"]),
                            "resume_overlap_days": RESUME_OVERLAP_DAYS if args.resume else 0,
                            "updated_at": datetime.now(UTC).isoformat()}
                        save_local_checkpoints(local_checkpoints)
                    else:
                        local_stats = {"files": 0, "skipped": [], "removed_ineligible": 0}
                    report["datasets"][item.symbol] = {**_quality(rows, "trade_date", item.source, time.monotonic()-tick),
                        "files_written": local_stats["files"], "skipped_current_session": [*skipped, *local_stats["skipped"]],
                        "removed_ineligible": local_stats["removed_ineligible"]}
                LOGGER.info("Completed %s | rows=%d", item.symbol, report["datasets"][item.symbol]["rows"])
            except Exception as exc:
                if connection:
                    connection.rollback()
                    repository.save_checkpoint(task, "failed", None, 0, str(exc))
                    connection.commit()
                elif args.storage == "local" and not args.dry_run:
                    previous = local_checkpoints["tasks"].get(task, {})
                    local_checkpoints["tasks"][task] = {**previous, "status": "failed", "error": str(exc),
                        "updated_at": datetime.now(UTC).isoformat()}
                    save_local_checkpoints(local_checkpoints)
                report["failures"].append({"dataset": item.symbol, "error": str(exc)})
                LOGGER.exception("Failed %s", item.symbol)
        for key in series:
            spec = SERIES[key]
            start = max(args.start or spec[3], spec[3], MAX_START)
            task = f"{spec[1]}:{key}"
            checkpoint = repository.checkpoint(task) if repository and args.resume else local_checkpoints["tasks"].get(task) if args.storage == "local" and args.resume else None
            start = resume_start(start, checkpoint, local=args.storage == "local")
            tick = time.monotonic()
            try:
                rows = fetch_fred(client, key, start, args.end)
                if repository:
                    sid = series_ids[key]
                    repository.upsert_many("macro_observations", [dict(row, macro_series_id=sid) for row in rows])
                    repository.save_checkpoint(task, "complete", args.end.isoformat(), len(rows))
                    connection.commit()
                    local_stats = {"files": 0}
                elif args.storage == "local" and not args.dry_run:
                    local_stats = write_local_macro(key, rows)
                    local_checkpoints["tasks"][task] = {"status": "complete", "last_cursor": args.end.isoformat(),
                        "rows_written": len(rows), "updated_at": datetime.now(UTC).isoformat()}
                    save_local_checkpoints(local_checkpoints)
                else:
                    local_stats = {"files": 0}
                report["datasets"][key] = {**_quality(rows, "observation_date", "fred", time.monotonic()-tick),
                    "files_written": local_stats["files"]}
                LOGGER.info("Completed %s | rows=%d", key, len(rows))
            except Exception as exc:
                if connection:
                    connection.rollback()
                elif args.storage == "local" and not args.dry_run:
                    previous = local_checkpoints["tasks"].get(task, {})
                    local_checkpoints["tasks"][task] = {**previous, "status": "failed", "error": str(exc),
                        "updated_at": datetime.now(UTC).isoformat()}
                    save_local_checkpoints(local_checkpoints)
                report["failures"].append({"dataset": key, "error": str(exc)})
                LOGGER.exception("Failed %s", key)
        if repository:
            report["table_counts"] = repository.table_counts()
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_database_size(current_database()) AS bytes")
                report["database_size_bytes"] = cursor.fetchone()["bytes"]
        report["total_duration_seconds"] = round(time.monotonic()-started, 3)
        _atomic_report(report)
        return 1 if report["failures"] else 0
    finally:
        if connection:
            connection.close()


if __name__ == "__main__":
    raise SystemExit(main())
