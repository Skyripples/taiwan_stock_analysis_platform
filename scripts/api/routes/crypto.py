from __future__ import annotations

from fastapi import APIRouter, HTTPException, Path, Query

from api.services.crypto import CRYPTO_RANGES, CRYPTO_SYMBOLS, get_crypto_candles, get_crypto_summary

router = APIRouter(prefix="/crypto", tags=["crypto"])


def valid_crypto_symbol(symbol: str) -> str:
    normalized = symbol.upper()
    if normalized not in CRYPTO_SYMBOLS:
        raise HTTPException(status_code=422, detail={"code": "INVALID_CRYPTO_SYMBOL", "message": "Supported symbols are BTC-USD and ETH-USD"})
    return normalized


@router.get("/summary")
def crypto_summary() -> dict:
    return get_crypto_summary()


@router.get("/{symbol}/candles")
def crypto_candles(
    symbol: str = Path(...),
    range_key: str = Query(default="24H", alias="range", pattern="^(1H|4H|24H|7D|30D)$"),
) -> dict:
    symbol = valid_crypto_symbol(symbol)
    if range_key not in CRYPTO_RANGES:
        raise HTTPException(status_code=422, detail={"code": "INVALID_CRYPTO_RANGE", "message": "Unsupported range"})
    return get_crypto_candles(symbol, range_key)
