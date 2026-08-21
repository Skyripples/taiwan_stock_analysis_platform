from __future__ import annotations

import re
import threading
import time
from typing import Any, Callable

from fastapi import APIRouter, HTTPException, Path, Query

from api.services.stocks import get_chips, get_financials, get_industry_peers, get_stock, search_stocks


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


@router.get("/stocks")
def stocks(
    search: str | None = Query(default=None, min_length=1, max_length=40),
    market: str | None = Query(default=None, pattern="^(TWSE|TPEx)$"),
    industry: str | None = Query(default=None, min_length=1, max_length=60),
    limit: int = Query(default=20, ge=1, le=100),
) -> dict:
    return {"count": len(rows := search_stocks(search, market, industry, limit)), "results": rows}


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
