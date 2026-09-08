from __future__ import annotations

import re
import threading
import time
from typing import Any, Callable

from fastapi import APIRouter, HTTPException, Path, Query

from api.services.stocks import (
    SCREENER_SORT_COLUMNS,
    get_chips,
    get_financials,
    get_industry_peers,
    get_screener_options,
    get_stock,
    screen_stocks,
    search_stocks,
)


router = APIRouter(tags=["stocks"])
SYMBOL = re.compile(r"^[0-9A-Z]{2,10}$")
_cache: dict[str, tuple[float, Any]] = {}
_lock = threading.Lock()


def cached(key: str, ttl: int, loader: Callable[[], Any]) -> Any:
    now = time.monotonic()
    with _lock:
        hit = _cache.get(key)
        if hit and hit[0] > now:
            return hit[1]
    result = loader()
    with _lock:
        _cache[key] = (now + ttl, result)
        if len(_cache) > 1000:
            expired = [name for name, item in _cache.items() if item[0] <= now]
            for name in expired: _cache.pop(name, None)
    return result


def valid_symbol(symbol: str) -> str:
    symbol = symbol.upper()
    if not SYMBOL.fullmatch(symbol):
        raise HTTPException(status_code=422, detail={"code": "INVALID_SYMBOL", "message": "Invalid stock symbol"})
    return symbol


def _validate_ranges(filters: dict[str, Any]) -> None:
    for prefix in ("price", "change_percent", "pe", "pb", "dividend_yield", "revenue_yoy", "roe", "debt_ratio", "foreign_5d"):
        minimum, maximum = filters.get(f"{prefix}_min"), filters.get(f"{prefix}_max")
        if minimum is not None and maximum is not None and minimum > maximum:
            raise HTTPException(
                status_code=422,
                detail={"code": "INVALID_RANGE", "message": f"{prefix}_min cannot exceed {prefix}_max"},
            )


@router.get("/stocks")
def stocks(
    search: str | None = Query(default=None, min_length=1, max_length=40),
    market: str | None = Query(default=None, pattern="^(TWSE|TPEx)$"),
    industry: str | None = Query(default=None, min_length=1, max_length=60),
    limit: int = Query(default=20, ge=1, le=100),
) -> dict:
    return {"count": len(rows := search_stocks(search, market, industry, limit)), "results": rows}


# Literal screener paths must stay above /stocks/{symbol} routes.
@router.get("/stocks/screener/options")
def screener_options() -> dict:
    return cached("screener:options", 300, get_screener_options)


@router.get("/stocks/screener")
def screener(
    search: str | None = Query(default=None, min_length=1, max_length=40),
    market: str | None = Query(default=None, pattern="^(TWSE|TPEx)$"),
    industry: str | None = Query(default=None, min_length=1, max_length=60),
    instrument_type: str | None = Query(default=None, min_length=1, max_length=30),
    price_min: float | None = None, price_max: float | None = None,
    change_percent_min: float | None = None, change_percent_max: float | None = None,
    pe_min: float | None = None, pe_max: float | None = None,
    pb_min: float | None = None, pb_max: float | None = None,
    dividend_yield_min: float | None = None, dividend_yield_max: float | None = None,
    revenue_yoy_min: float | None = None, revenue_yoy_max: float | None = None,
    roe_min: float | None = None, roe_max: float | None = None,
    debt_ratio_min: float | None = None, debt_ratio_max: float | None = None,
    foreign_5d_min: float | None = None, foreign_5d_max: float | None = None,
    sort: str = Query(default="symbol", pattern="^(symbol|close|change_percent|pe|pb|dividend_yield|revenue_yoy|roe|debt_ratio|foreign_5d)$"),
    order: str = Query(default="asc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> dict:
    filters = {key: item for key, item in locals().items()
               if key not in {"sort", "order", "limit", "offset"} and item is not None}
    _validate_ranges(filters)
    # Defensive assertion keeps dynamic ORDER BY identifiers constrained even if validation changes later.
    if sort not in SCREENER_SORT_COLUMNS:
        raise HTTPException(status_code=422, detail={"code": "INVALID_SORT", "message": "Invalid sort field"})
    return screen_stocks(filters, sort, order, limit, offset)


@router.get("/stocks/{symbol}/financials")
def financials(symbol: str = Path(...), limit: int = Query(default=12, ge=1, le=20)) -> dict:
    symbol = valid_symbol(symbol)
    result = get_financials(symbol, limit)
    if result is None:
        raise HTTPException(status_code=404, detail={"code": "STOCK_NOT_FOUND", "message": "Stock was not found"})
    return result


@router.get("/stocks/{symbol}/chips")
def chips(symbol: str = Path(...), limit: int = Query(default=60, ge=1, le=250)) -> dict:
    symbol = valid_symbol(symbol)
    result = get_chips(symbol, limit)
    if result is None:
        raise HTTPException(status_code=404, detail={"code": "STOCK_NOT_FOUND", "message": "Stock was not found"})
    return result


@router.get("/stocks/{symbol}")
def stock_detail(symbol: str = Path(...)) -> dict:
    symbol = valid_symbol(symbol)
    result = cached(f"stock:{symbol}", 45, lambda: get_stock(symbol))
    if result is None:
        raise HTTPException(status_code=404, detail={"code": "STOCK_NOT_FOUND", "message": "Stock was not found"})
    return result


@router.get("/industries/{industry}/peers")
def industry_peers(industry: str = Path(..., min_length=1, max_length=60)) -> dict:
    return cached(f"industry:{industry}", 60, lambda: get_industry_peers(industry))
