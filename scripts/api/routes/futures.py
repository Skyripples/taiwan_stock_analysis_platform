from datetime import date

from fastapi import APIRouter, HTTPException, Query

from api.services.futures import get_futures_history, get_futures_intraday, get_futures_summary

router = APIRouter(prefix="/futures", tags=["futures"])

@router.get("/summary")
def futures_summary() -> dict:
    return get_futures_summary()


@router.get("/history")
def futures_history(
    product: str = Query("TMF", pattern="^(TX|MTX|TMF)$"),
    contract: str | None = Query(None, max_length=16, pattern="^(TX|MTX|TMF)[0-9]{6}$"),
    range: str = Query("3M", pattern="^(1M|3M|6M|1Y|3Y|5Y)$"),
    start: date | None = None,
    end: date | None = None,
    limit: int = Query(500, ge=1, le=1500),
) -> dict:
    if start and end and start > end:
        raise HTTPException(422, {"code": "INVALID_RANGE", "message": "start cannot exceed end"})
    return get_futures_history(product, contract, range, start, end, limit)


@router.get("/intraday")
def futures_intraday(
    product: str = Query("TX", pattern="^(TX|MTX|TMF)$"),
    range: str = Query("1D", pattern="^(1H|4H|1D|3D|7D)$"),
    interval: str = Query("5m", pattern="^5m$"),
    limit: int = Query(2200, ge=1, le=2200),
) -> dict:
    return get_futures_intraday(product, range, limit)
