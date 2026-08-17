"""Utilities for resolving Taiwan Stock Exchange trading dates."""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from config import PROJECT_ROOT


DEFAULT_CALENDAR_PATH = PROJECT_ROOT / "data" / "calendar" / "twse_trading_calendar.json"


def load_trading_calendar(path: Path = DEFAULT_CALENDAR_PATH) -> dict[str, Any]:
    """Load and minimally validate the versioned TWSE calendar."""

    with path.open("r", encoding="utf-8") as source:
        calendar = json.load(source)
    if not isinstance(calendar, dict) or calendar.get("market") != "TWSE":
        raise ValueError("Invalid TWSE trading calendar")
    if not isinstance(calendar.get("years"), dict):
        raise ValueError("TWSE trading calendar does not contain yearly data")
    return calendar


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
        yearly = years.get(str(candidate.year))
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
        if candidate < coverage_start or candidate > coverage_end:
            return None
        iso_date = candidate.isoformat()
        if iso_date in special_open or (candidate.isoweekday() in weekdays and iso_date not in closed):
            return iso_date
        candidate += timedelta(days=1)
