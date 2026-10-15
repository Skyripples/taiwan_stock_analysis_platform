"""Read-only futures dashboard queries."""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

from api.db import pool
from futures_market import basis, price_oi_state

RANGE_MONTHS = {"1M": 1, "3M": 3, "6M": 6, "1Y": 12, "3Y": 36, "5Y": 60}


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
