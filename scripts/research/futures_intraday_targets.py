"""Read-only small-sample builders for the four V5 TX intraday targets.

The official daily tick archive is suitable for realised labels only.  Its
``available_at`` belongs to the completed daily source file and is deliberately
not presented as a contemporaneous intraday feature vintage.
"""
from __future__ import annotations

import json
from functools import lru_cache
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from config import PROJECT_ROOT
from futures_intraday_reconstruction import contract_expiry
from trading_calendar import get_trading_days_in_range

UTC = timezone.utc
TAIPEI = ZoneInfo("Asia/Taipei")
NEW_YORK = ZoneInfo("America/New_York")
INTERVAL = timedelta(minutes=5)
INTRADAY_ROOT = PROJECT_ROOT / "data_lake" / "staging" / "futures_intraday_5m"
US_CALENDAR_PATH = PROJECT_ROOT / "config" / "us_equity_calendar_2026.json"

US_OPEN_TO_1H = "us_open_to_1h_tx_return"
US_OPEN_TO_NIGHT_CLOSE = "us_open_to_night_close_tx_return"
REGULAR_OPEN_TO_1H = "regular_open_to_1h_tx_return"
REGULAR_OPEN_TO_CLOSE = "regular_open_to_close_tx_return"
TARGET_NAMES = (US_OPEN_TO_1H, US_OPEN_TO_NIGHT_CLOSE, REGULAR_OPEN_TO_1H, REGULAR_OPEN_TO_CLOSE)


@dataclass(frozen=True)
class UsSession:
    trade_date: date
    open_at: datetime
    close_at: datetime
    early_close: bool


def aware(value: Any, field: str) -> datetime:
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value.astimezone(UTC)


def load_us_calendar(path: Path = US_CALENDAR_PATH) -> dict[str, Any]:
    with path.open(encoding="utf-8") as source:
        calendar = json.load(source)
    if calendar.get("market") != "NYSE" or calendar.get("timezone") != "America/New_York":
        raise ValueError("invalid US equity calendar")
    if not calendar.get("version") or not calendar.get("source", {}).get("url"):
        raise ValueError("US equity calendar requires versioned provenance")
    return calendar


def us_regular_session(day: date, calendar: dict[str, Any]) -> UsSession | None:
    """Return a verified NYSE session; uncovered dates fail closed."""
    year = calendar.get("years", {}).get(str(day.year))
    if not isinstance(year, dict):
        return None
    try:
        start = date.fromisoformat(year["coverage_start"])
        end = date.fromisoformat(year["coverage_end"])
        weekdays = {int(item) for item in year["regular_trading_weekdays"]}
        closed = {date.fromisoformat(item) for item in year["closed_dates"]}
        early = {date.fromisoformat(item) for item in year.get("early_close_dates", [])}
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("invalid US equity calendar year") from error
    if not start <= day <= end or day.isoweekday() not in weekdays or day in closed:
        return None
    open_at = datetime.combine(day, time(9, 30), NEW_YORK).astimezone(UTC)
    close_at = datetime.combine(day, time(13 if day in early else 16), NEW_YORK).astimezone(UTC)
    return UsSession(day, open_at, close_at, day in early)


def read_tx_candles(root: Path = INTRADAY_ROOT) -> list[dict[str, Any]]:
    """Read physical TX files only; no data is written or reconstructed here."""
    import pyarrow.parquet as pq

    rows: list[dict[str, Any]] = []
    for path in sorted(root.glob("trading_date=*/product=TX/candles.parquet")):
        rows.extend(pq.ParquetFile(path).read().to_pylist())
    for row in rows:
        row["timestamp_utc"] = aware(row["timestamp_utc"], "timestamp_utc")
        row["available_at"] = aware(row["available_at"], "available_at")
        row["fetched_at"] = aware(row["fetched_at"], "fetched_at")
        if row.get("product") != "TX" or row.get("interval_seconds") != 300:
            raise ValueError("expected TX five-minute candles")
    return rows


def _close_at(rows: Iterable[dict[str, Any]], endpoint: datetime, contract: str, session: str) -> dict[str, Any] | None:
    """Return the completed five-minute bar whose close is exactly ``endpoint``."""
    start = endpoint - INTERVAL
    for row in rows:
        if row["contract"] == contract and row["session"] == session and row["timestamp_utc"] == start:
            return row
    return None


def _open_at(rows: Iterable[dict[str, Any]], start: datetime, contract: str) -> dict[str, Any] | None:
    for row in rows:
        if row["contract"] == contract and row["session"] == "regular" and row["timestamp_utc"] == start:
            return row
    return None


def _ratio(base: dict[str, Any], endpoint: dict[str, Any], base_key: str = "close") -> float | None:
    try:
        denominator, numerator = float(base[base_key]), float(endpoint["close"])
    except (KeyError, TypeError, ValueError):
        return None
    return numerator / denominator - 1.0 if denominator else None


def _front_contract_before(rows: Iterable[dict[str, Any]], cutoff: datetime, trading_date: date) -> str | None:
    """Deterministically choose the nearest non-expired contract known before cutoff.

    This deliberately uses neither final-session volume nor endpoint prices.
    """
    candidates: set[str] = set()
    for row in rows:
        if row["timestamp_utc"] + INTERVAL <= cutoff and row.get("contract"):
            try:
                if expiry_for_contract(row["contract"]) >= trading_date:
                    candidates.add(row["contract"])
            except (TypeError, ValueError):
                continue
    return min(candidates, key=lambda item: (expiry_for_contract(item), item)) if candidates else None


@lru_cache(maxsize=None)
def expiry_for_contract(contract: str) -> date:
    """Cache deterministic calendar expiry resolution for repeated 5m candles."""
    return contract_expiry(contract)


def _record(name: str, prediction_at: datetime, trading_date: date, contract: str | None,
            base_at: datetime | None, endpoint_at: datetime | None, base: dict[str, Any] | None,
            endpoint: dict[str, Any] | None, *, base_key: str = "close", reason: str | None = None) -> dict[str, Any]:
    if reason is None and (base is None or endpoint is None):
        reason = "missing_required_price"
    available = None
    value = None
    if reason is None:
        available = max(aware(base["available_at"], "base.available_at"), aware(endpoint["available_at"], "endpoint.available_at"))
        value = _ratio(base, endpoint, base_key)
        if value is None:
            reason = "invalid_required_price"
    return {
        "target_name": name, "prediction_at": prediction_at, "trading_date": trading_date,
        "contract": contract, "base_at": base_at, "endpoint_at": endpoint_at,
        "target_return": value, "target_available_at": available, "missing_reason": reason,
    }


def _us_anchor_date(rows: list[dict[str, Any]]) -> date | None:
    starts = [row["timestamp_utc"].astimezone(TAIPEI).date() for row in rows
              if row["timestamp_utc"].astimezone(TAIPEI).time() >= time(15)]
    return min(starts) if starts else None


def build_intraday_targets(candles: Iterable[dict[str, Any]], us_calendar: dict[str, Any],
                           taiwan_sessions: set[date] | None = None) -> list[dict[str, Any]]:
    """Build null-preserving realised labels for all four requested tasks."""
    rows = [dict(row) for row in candles]
    for row in rows:
        row["timestamp_utc"] = aware(row["timestamp_utc"], "timestamp_utc")
        row["available_at"] = aware(row["available_at"], "available_at")
        if not isinstance(row.get("trading_date"), date):
            raise ValueError("trading_date must be a date")
    groups: dict[date, list[dict[str, Any]]] = {}
    for row in rows:
        if row.get("product") == "TX":
            groups.setdefault(row["trading_date"], []).append(row)
    output: list[dict[str, Any]] = []
    for trading_date, day_rows in sorted(groups.items()):
        if taiwan_sessions is not None and trading_date not in taiwan_sessions:
            continue
        after = [row for row in day_rows if row["session"] == "after_hours"]
        regular = [row for row in day_rows if row["session"] == "regular"]

        us_day = _us_anchor_date(after)
        session = us_regular_session(us_day, us_calendar) if us_day else None
        if session is None:
            prediction = datetime.combine(us_day or trading_date, time(9, 15), NEW_YORK).astimezone(UTC)
            for name in (US_OPEN_TO_1H, US_OPEN_TO_NIGHT_CLOSE):
                output.append(_record(name, prediction, trading_date, None, None, None, None, None,
                                      reason="unverified_or_closed_us_session"))
        else:
            prediction = session.open_at - timedelta(minutes=15)
            contract = _front_contract_before(after, prediction, trading_date)
            base = _close_at(after, session.open_at, contract, "after_hours") if contract else None
            one_hour = _close_at(after, session.open_at + timedelta(hours=1), contract, "after_hours") if contract else None
            # The Friday night session closes on Saturday 05:00 Taipei even
            # though its associated *trading_date* is Monday.  Derive the
            # calendar endpoint from the US-open evening, never by adding a
            # day to the trading-date label.
            night_close_at = datetime.combine(session.trade_date + timedelta(days=1), time(5), TAIPEI).astimezone(UTC)
            night_close = _close_at(after, night_close_at, contract, "after_hours") if contract else None
            output.append(_record(US_OPEN_TO_1H, prediction, trading_date, contract, session.open_at,
                                  session.open_at + timedelta(hours=1), base, one_hour))
            output.append(_record(US_OPEN_TO_NIGHT_CLOSE, prediction, trading_date, contract, session.open_at,
                                  night_close_at, base, night_close))

        prediction = datetime.combine(trading_date, time(8, 30), TAIPEI).astimezone(UTC)
        contract = _front_contract_before(after, prediction, trading_date)
        open_at = datetime.combine(trading_date, time(8, 45), TAIPEI).astimezone(UTC)
        base = _open_at(regular, open_at, contract) if contract else None
        one_hour = _close_at(regular, datetime.combine(trading_date, time(9, 45), TAIPEI).astimezone(UTC), contract, "regular") if contract else None
        try:
            close_wall = time(13, 30) if contract and expiry_for_contract(contract) == trading_date else time(13, 45)
        except ValueError:
            close_wall = time(13, 45)
        close_at = datetime.combine(trading_date, close_wall, TAIPEI).astimezone(UTC)
        day_close = _close_at(regular, close_at, contract, "regular") if contract else None
        output.append(_record(REGULAR_OPEN_TO_1H, prediction, trading_date, contract, open_at,
                              datetime.combine(trading_date, time(9, 45), TAIPEI).astimezone(UTC), base, one_hour,
                              base_key="open"))
        output.append(_record(REGULAR_OPEN_TO_CLOSE, prediction, trading_date, contract, open_at, close_at, base,
                              day_close, base_key="open"))
    return output


def actual_taiwan_sessions(rows: Iterable[dict[str, Any]]) -> set[date]:
    days = {row["trading_date"] for row in rows}
    if not days:
        return set()
    sessions, _ = get_trading_days_in_range(min(days), max(days))
    return set(sessions)
