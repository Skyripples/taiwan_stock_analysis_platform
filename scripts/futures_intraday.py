"""Pure tick aggregation and TAIFEX session helpers for 5-minute futures candles."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Callable
from zoneinfo import ZoneInfo

from futures_market import PRODUCTS
from trading_calendar import get_next_trading_day, get_trading_days_in_range

TAIPEI = ZoneInfo("Asia/Taipei")
UTC = timezone.utc
INTERVAL_SECONDS = 300
REGULAR_START, REGULAR_END = time(8, 45), time(13, 45)
AFTERNOON_START, MORNING_END = time(15, 0), time(5, 0)


@dataclass(frozen=True)
class FuturesTick:
    timestamp: datetime
    product: str
    contract: str
    price: Decimal
    volume: int | None
    cumulative_volume: int | None = None


@dataclass
class FuturesCandle:
    timestamp_utc: datetime
    trading_date: date
    product: str
    contract: str
    session: str
    interval_seconds: int
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int
    source: str
    available_at: datetime
    fetched_at: datetime
    days_to_expiry: int | None = None

    def validate(self) -> None:
        if self.product not in PRODUCTS or not self.contract:
            raise ValueError("invalid futures product or contract")
        if self.timestamp_utc.tzinfo is None or self.timestamp_utc.utcoffset() != timedelta(0):
            raise ValueError("timestamp_utc must be timezone-aware UTC")
        if int(self.timestamp_utc.timestamp()) % self.interval_seconds:
            raise ValueError("timestamp is not aligned to the candle boundary")
        if self.high < max(self.open, self.close) or self.low > min(self.open, self.close):
            raise ValueError("invalid OHLC relationship")
        if self.volume < 0:
            raise ValueError("volume cannot be negative")

    def row(self) -> dict:
        self.validate()
        return asdict(self)


def _is_trading_day(value: date) -> bool:
    sessions, _ = get_trading_days_in_range(value, value)
    return value in sessions


def resolve_session(timestamp: datetime) -> tuple[str, date]:
    if timestamp.tzinfo is None:
        raise ValueError("tick timestamp must be timezone-aware")
    local = timestamp.astimezone(TAIPEI)
    wall = local.time().replace(tzinfo=None)
    if REGULAR_START <= wall <= REGULAR_END:
        if not _is_trading_day(local.date()):
            raise ValueError("regular tick is outside an official trading day")
        return "regular", local.date()
    if wall >= AFTERNOON_START:
        next_day = get_next_trading_day(local.date())
        if not next_day:
            raise ValueError("trading calendar cannot resolve after-hours trading date")
        return "after_hours", date.fromisoformat(next_day)
    if wall <= MORNING_END:
        if not _is_trading_day(local.date()):
            raise ValueError("after-hours tick has no covered trading date")
        return "after_hours", local.date()
    raise ValueError("tick is outside TX/MTX/TMF trading sessions")


def bucket_start(timestamp: datetime) -> datetime:
    utc = timestamp.astimezone(UTC)
    return utc.replace(minute=utc.minute - utc.minute % 5, second=0, microsecond=0)


class CandleAggregator:
    def __init__(self, source: str = "TAIFEX official tick CSV"):
        self.source = source
        self._forming: dict[tuple[str, str, str], FuturesCandle] = {}
        self._last_total: dict[tuple[str, str], int] = {}

    def _tick_volume(self, tick: FuturesTick) -> int:
        key = (tick.product, tick.contract)
        if tick.cumulative_volume is not None:
            previous = self._last_total.get(key)
            self._last_total[key] = tick.cumulative_volume
            if previous is not None and tick.cumulative_volume >= previous:
                return tick.cumulative_volume - previous
        if tick.volume is None:
            raise ValueError("tick volume is unavailable")
        return tick.volume

    def add(self, tick: FuturesTick, *, fetched_at: datetime | None = None) -> list[FuturesCandle]:
        if tick.product not in PRODUCTS or not tick.contract:
            raise ValueError("invalid tick product or contract")
        session, trading_date = resolve_session(tick.timestamp)
        start = bucket_start(tick.timestamp)
        key = (tick.product, tick.contract, session)
        completed: list[FuturesCandle] = []
        current = self._forming.get(key)
        if current and start > current.timestamp_utc:
            current.available_at = current.timestamp_utc + timedelta(seconds=INTERVAL_SECONDS)
            current.validate()
            completed.append(current)
            current = None
        if current and start < current.timestamp_utc:
            return completed
        volume = self._tick_volume(tick)
        now = (fetched_at or datetime.now(UTC)).astimezone(UTC)
        if current is None:
            current = FuturesCandle(start, trading_date, tick.product, tick.contract, session,
                                    INTERVAL_SECONDS, tick.price, tick.price, tick.price, tick.price,
                                    volume, self.source, start + timedelta(seconds=INTERVAL_SECONDS), now)
            self._forming[key] = current
        else:
            current.high = max(current.high, tick.price)
            current.low = min(current.low, tick.price)
            current.close = tick.price
            current.volume += volume
            current.fetched_at = now
        return completed

    def flush_completed(self, now: datetime) -> list[FuturesCandle]:
        utc_now = now.astimezone(UTC)
        completed = []
        for key, candle in list(self._forming.items()):
            if candle.timestamp_utc + timedelta(seconds=INTERVAL_SECONDS) <= utc_now:
                candle.available_at = candle.timestamp_utc + timedelta(seconds=INTERVAL_SECONDS)
                candle.validate()
                completed.append(candle)
                del self._forming[key]
        return completed
