"""Read-only futures dashboard queries."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from api.db import pool
from config import PROJECT_ROOT
from futures_market import basis, price_oi_state

RANGE_MONTHS = {"1M": 1, "3M": 3, "6M": 6, "1Y": 12, "3Y": 36, "5Y": 60}
INTRADAY_RANGES = {"1H": timedelta(hours=1), "4H": timedelta(hours=4),
                   "1D": timedelta(days=1), "3D": timedelta(days=3), "7D": timedelta(days=7)}
RESEARCH_DATASET_ROOT = PROJECT_ROOT / "data_lake" / "futures_prediction" / "research_dataset"

# This is deliberately a public schema, not a passthrough of the research parquet.
RESEARCH_PUBLIC_COLUMNS = {
    "prediction_date": "prediction_date",
    "prediction_at": "prediction_at",
    "tx_contract": "tx_contract",
    "tx_return": "tx_previous_day_return",
    "tx_open_to_close_return": "tx_open_to_close_return",
    "tx_intraday_range": "tx_intraday_range_pct",
    "tx_true_range": "tx_true_range_pct",
    "tx_return_volatility_5d": "tx_return_volatility_5d",
    "tx_volume": "tx_volume",
    "tx_open_interest": "tx_open_interest",
    "tx_front_next_spread": "tx_front_next_spread",
    "tx_front_next_spread_pct": "tx_front_next_spread_pct",
    "tx_basis": "tx_basis",
    "tx_basis_pct": "tx_basis_pct",
    "days_to_expiry": "tx_days_to_expiry",
    "expiry_week": "tx_expiry_week",
    "roll_regime": "tx_roll_regime",
    "foreign_net_oi": "foreign_net_oi",
    "foreign_net_oi_change": "foreign_net_oi_change",
    "investment_trust_net_oi": "investment_trust_net_oi",
    "investment_trust_net_oi_change": "investment_trust_net_oi_change",
    "dealer_net_oi": "dealer_net_oi",
    "dealer_net_oi_change": "dealer_net_oi_change",
    "volume_pcr": "volume_pcr",
    "oi_pcr": "oi_pcr",
    "sox": "sox_value",
    "tsm_adr": "tsm_adr_value",
    "nasdaq": "nasdaq_value",
    "sp500": "sp500_value",
    "usdtwd": "usdtwd_value",
    "us10y": "dgs10_value",
    "target_trading_date": "target_trading_date",
    "next_trading_day_regular_return": "next_trading_day_regular_return",
    "direction_class": "direction_class",
}


def _clean(value: Any) -> Any:
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral() else float(value)
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def _row(row: dict[str, Any] | None) -> dict[str, Any] | None:
    return None if row is None else {key: _clean(value) for key, value in row.items()}


def _decorate(item: dict[str, Any], spot: float | None) -> dict[str, Any]:
    clean = _row(item) or {}
    b, bp = basis(clean.get("close"), spot)
    clean.update({"basis": b, "basis_pct": bp,
                  "price_oi_state": price_oi_state(clean.get("change"), clean.get("oi_change"))})
    return clean


def _research_rows(root: Path = RESEARCH_DATASET_ROOT) -> list[dict[str, Any]]:
    """Read local research data and reject duplicate canonical prediction dates."""
    import pyarrow.parquet as pq

    rows: list[dict[str, Any]] = []
    seen: set[date] = set()
    for path in sorted(root.glob("partition_year=*/part-00000.parquet")):
        for row in pq.ParquetFile(path).read().to_pylist():
            prediction_date = row["prediction_date"]
            if prediction_date in seen:
                raise ValueError(f"Duplicate futures research prediction_date: {prediction_date}")
            seen.add(prediction_date)
            rows.append(row)
    return sorted(rows, key=lambda item: item["prediction_date"])


def _public_research_row(row: dict[str, Any]) -> dict[str, Any]:
    public = {name: _clean(row.get(source)) for name, source in RESEARCH_PUBLIC_COLUMNS.items()}
    flags = row.get("quality_flags")
    public["quality_flags"] = json.loads(flags) if isinstance(flags, str) else flags
    return public


def get_futures_research_summary() -> dict[str, Any]:
    rows = _research_rows()
    versions = {row.get("research_dataset_version") for row in rows if row.get("research_dataset_version")}
    if len(versions) > 1:
        raise ValueError("Multiple futures research dataset versions found")
    coverage_columns = {
        "tx_front_next_spread": "tx_front_next_spread",
        "tx_basis": "tx_basis",
        "foreign_net_oi": "foreign_net_oi",
        "investment_trust_net_oi": "investment_trust_net_oi",
        "dealer_net_oi": "dealer_net_oi",
        "volume_pcr": "volume_pcr",
        "oi_pcr": "oi_pcr",
    }
    counts = {name: sum(row.get(column) is not None for row in rows) for name, column in coverage_columns.items()}
    direction_counts = {name: sum(row.get("direction_class") == name for row in rows)
                        for name in ("up", "neutral", "down")}
    return {
        "research_dataset_version": next(iter(versions), None),
        "rows": len(rows),
        "first_prediction_date": _clean(rows[0]["prediction_date"]) if rows else None,
        "last_prediction_date": _clean(rows[-1]["prediction_date"]) if rows else None,
        "target_available": sum(row.get("next_trading_day_regular_return") is not None for row in rows),
        "target_missing": sum(row.get("next_trading_day_regular_return") is None for row in rows),
        "class_distribution": direction_counts,
        "feature_coverage": {
            name: {"available": available, "missing": len(rows) - available}
            for name, available in counts.items()
        },
    }


def get_futures_research_daily(start: date | None, end: date | None, limit: int) -> dict[str, Any]:
    rows = _research_rows()
    filtered = [row for row in rows if (start is None or row["prediction_date"] >= start)
                and (end is None or row["prediction_date"] <= end)]
    results = [_public_research_row(row) for row in filtered[:limit]]
    return {"count": len(results), "start": _clean(start), "end": _clean(end), "limit": limit, "results": results}


def get_futures_summary() -> dict[str, Any]:
    with pool.connection() as connection, connection.cursor() as cursor:
        cursor.execute(
            """WITH series AS (
                   SELECT f.*,
                          f.close-lag(f.close) OVER
                            (PARTITION BY product,contract,session ORDER BY trade_date) AS change,
                          f.open_interest-lag(f.open_interest) OVER
                            (PARTITION BY product,contract,session ORDER BY trade_date) AS oi_change
                     FROM futures_daily_prices f
                    WHERE product=ANY(%s) AND session='regular'
                 ), latest_day AS (
                   SELECT product,max(trade_date) AS trade_date FROM series GROUP BY product
                 )
                 SELECT s.* FROM series s JOIN latest_day l USING(product,trade_date)
                  WHERE s.contract_rank IN ('front','next')
                  ORDER BY CASE s.product WHEN 'TMF' THEN 1 WHEN 'MTX' THEN 2 ELSE 3 END,
                           CASE s.contract_rank WHEN 'front' THEN 1 ELSE 2 END""",
            (["TX", "MTX", "TMF"],),
        )
        products = cursor.fetchall()
        spot = None
        tx_trade_date = next((row["trade_date"] for row in products if row["product"] == "TX"), None)
        try:
            cursor.execute(
                """SELECT p.close FROM market_daily_prices p
                     JOIN market_instruments i USING(instrument_id)
                    WHERE i.canonical_symbol=ANY(%s)
                      AND p.trade_date=%s
                    LIMIT 1""",
                (["TAIEX", "^TWII"], tx_trade_date),
            )
            spot_row = cursor.fetchone()
            spot = float(spot_row["close"]) if spot_row and spot_row["close"] is not None else None
        except Exception:
            connection.rollback()
    decorated = [_decorate(item, spot) for item in products]
    return {
        "products": decorated, "taiex": spot, "source": "TAIFEX",
        "latest_trade_date": max((x["trade_date"] for x in decorated), default=None),
        "unavailable": ["institutional_positions", "put_call_ratio", "intraday_5m", "night_history"],
    }


def get_futures_history(product: str, contract: str | None, range_key: str,
                        start: date | None, end: date | None, limit: int) -> dict[str, Any]:
    where = ["product=%s", "session='regular'"]
    params: list[Any] = [product]
    if contract:
        where.append("contract=%s")
        params.append(contract)
    else:
        # Raw front-month observations only; not a back-adjusted continuous series.
        where.append("contract_rank='front'")
    if start:
        where.append("trade_date >= %s")
        params.append(start)
    else:
        where.append("trade_date >= (CURRENT_DATE - (%s * INTERVAL '1 month'))")
        params.append(RANGE_MONTHS[range_key])
    if end:
        where.append("trade_date <= %s")
        params.append(end)
    params.append(limit)
    query = f"""SELECT trade_date,product,contract,contract_rank,open,high,low,close,
                       settlement_price,volume,open_interest,expiry_date,days_to_expiry,
                       source,source_date,available_at,fetched_at
                  FROM futures_daily_prices WHERE {' AND '.join(where)}
                 ORDER BY trade_date DESC LIMIT %s"""
    with pool.connection() as connection, connection.cursor() as cursor:
        cursor.execute(query, tuple(params))
        rows = [_row(row) for row in cursor.fetchall()]
    rows.reverse()
    return {"product": product, "contract": contract, "range": range_key,
            "count": len(rows), "results": rows}


def get_futures_intraday(product: str, range_key: str, limit: int) -> dict[str, Any]:
    with pool.connection() as connection, connection.cursor() as cursor:
        cursor.execute(
            """WITH latest AS (
                   SELECT max(timestamp_utc) AS stamp FROM futures_intraday_prices
                    WHERE product=%s AND interval_seconds=300
               ) SELECT * FROM (
                   SELECT i.product,i.contract,i.session,i.timestamp_utc AS timestamp,i.trading_date,
                          i.open,i.high,i.low,i.close,i.volume,i.source,i.available_at,i.fetched_at
                     FROM futures_intraday_prices i
                     JOIN futures_daily_prices d
                       ON d.product=i.product AND d.contract=i.contract AND d.trade_date=i.trading_date
                      AND d.session='regular' AND d.contract_rank='front'
                     CROSS JOIN latest
                    WHERE i.product=%s AND i.interval_seconds=300
                      AND i.timestamp_utc >= latest.stamp - %s
                    ORDER BY timestamp_utc DESC LIMIT %s
               ) recent ORDER BY timestamp ASC""",
            (product, product, INTRADAY_RANGES[range_key], limit),
        )
        rows = [_row(row) for row in cursor.fetchall()]
    latest = rows[-1] if rows else None
    latest_timestamp = latest.get("timestamp") if latest else None
    latest_trading_date = latest.get("trading_date") if latest else None
    taipei_today = datetime.now(timezone.utc).astimezone(ZoneInfo("Asia/Taipei")).date()
    stale = not latest_trading_date or (taipei_today - date.fromisoformat(latest_trading_date)).days > 4
    return {"product": product, "interval": "5m", "range": range_key, "count": len(rows),
            "results": rows, "latest_timestamp": latest_timestamp, "latest_session": latest.get("session") if latest else None,
            "latest_trading_date": latest_trading_date, "source": "TAIFEX",
            "update_mode": "daily_official_reconstruction", "stale": stale}
