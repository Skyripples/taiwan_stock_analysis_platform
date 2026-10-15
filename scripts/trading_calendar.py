"""Utilities for resolving Taiwan Stock Exchange trading dates."""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from config import PROJECT_ROOT


DEFAULT_CALENDAR_PATH = PROJECT_ROOT / "data" / "calendar" / "twse_trading_calendar.json"
DEFAULT_PIT_SESSIONS_GLOB = PROJECT_ROOT / "data_lake" / "taiwan_pit" / "market_sessions" / "**" / "*.parquet"


def load_trading_calendar(path: Path = DEFAULT_CALENDAR_PATH) -> dict[str, Any]:
    """Load and minimally validate the versioned TWSE calendar."""

    with path.open("r", encoding="utf-8") as source:
        calendar = json.load(source)
    if not isinstance(calendar, dict) or calendar.get("market") != "TWSE":
        raise ValueError("Invalid TWSE trading calendar")
    if not isinstance(calendar.get("years"), dict):
        raise ValueError("TWSE trading calendar does not contain yearly data")
    return calendar


def _versioned_session_status(value: date, calendar: dict[str, Any]) -> bool | None:
    yearly = calendar.get("years", {}).get(str(value.year))
    if not isinstance(yearly, dict):
        return None
    try:
        coverage_start = date.fromisoformat(yearly["coverage_start"])
        coverage_end = date.fromisoformat(yearly["coverage_end"])
        weekdays = {int(day) for day in yearly["regular_trading_weekdays"]}
        closed = set(yearly["closed_dates"])
        special_open = set(yearly["special_open_dates"])
    except (KeyError, TypeError, ValueError):
        return None
    if not coverage_start <= value <= coverage_end:
        return None
    key = value.isoformat()
    return key in special_open or (value.isoweekday() in weekdays and key not in closed)


def get_trading_days_in_range(
    start: date,
    end: date,
    *,
    pit_glob: Path = DEFAULT_PIT_SESSIONS_GLOB,
    calendar: dict[str, Any] | None = None,
) -> tuple[list[date], list[date]]:
    """Resolve sessions from PIT data, using the versioned calendar outside PIT coverage.

    Every date must be covered by one of the two official calendar stores. Missing
    coverage fails closed instead of assuming that weekdays are trading days.
    """
    if start > end:
        raise ValueError("start cannot exceed end")
    pit_rows: dict[date, bool] = {}
    files = list(pit_glob.parent.parent.rglob("*.parquet")) if "**" in str(pit_glob) else ([pit_glob] if pit_glob.exists() else [])
    if files:
        import duckdb
        connection = duckdb.connect()
        try:
            pattern = str(pit_glob).replace("\\", "/")
            rows = connection.execute(
                "SELECT calendar_date,is_trading_day FROM read_parquet(?,union_by_name=true) "
                "WHERE lower(market)='twse_tpex' AND calendar_date BETWEEN ? AND ? ORDER BY calendar_date",
                [pattern, start, end],
            ).fetchall()
            pit_rows = {row[0]: bool(row[1]) for row in rows}
        finally:
            connection.close()
    versioned = calendar if calendar is not None else load_trading_calendar()
    sessions: list[date] = []
    skipped: list[date] = []
    uncovered: list[date] = []
    cursor = start
    while cursor <= end:
        status = pit_rows.get(cursor)
        if status is None and cursor not in pit_rows:
            status = _versioned_session_status(cursor, versioned)
        if status is None:
            uncovered.append(cursor)
        else:
            (sessions if status else skipped).append(cursor)
        cursor += timedelta(days=1)
    if uncovered:
        raise ValueError(
            "Taiwan trading calendar coverage is missing "
            f"{uncovered[0].isoformat()}..{uncovered[-1].isoformat()}"
        )
    return sessions, skipped


def get_next_trading_day(value: str | date, calendar: dict[str, Any] | None = None) -> str | None:
    """Return the next official covered TWSE session, or ``None`` if unknown."""

    try:
        current = date.fromisoformat(value) if isinstance(value, str) else value
    except (TypeError, ValueError):
        return None
    if not isinstance(current, date):
        return None
    try:
        source = calendar if calendar is not None else load_trading_calendar()
        years = source["years"]
    except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError):
        return None

    candidate = current + timedelta(days=1)
    while True:
        status = _versioned_session_status(candidate, source)
        if status is None:
            return None
        if status:
            return candidate.isoformat()
        candidate += timedelta(days=1)
