"""TAIFEX session calendar with PIT-provenanced temporary closure overrides."""
from __future__ import annotations

import json
from datetime import date, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any

from config import PROJECT_ROOT
from trading_calendar import get_trading_days_in_range


CLOSURES_PATH = PROJECT_ROOT / "config" / "taifex_temporary_closures.json"


@lru_cache(maxsize=1)
def load_temporary_closures(path: Path = CLOSURES_PATH) -> dict[date, dict[str, Any]]:
    """Load explicit TAIFEX-only closures; missing/invalid input fails closed."""
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("market") != "TAIFEX" or not isinstance(payload.get("closures"), list):
        raise ValueError("Invalid TAIFEX temporary-closure configuration")
    result: dict[date, dict[str, Any]] = {}
    for item in payload["closures"]:
        day = date.fromisoformat(item["date"])
        sessions = item.get("sessions")
        if not isinstance(sessions, list) or not set(sessions).issubset({"regular", "after_hours"}):
            raise ValueError(f"Invalid TAIFEX closure sessions for {day}")
        available_at = item.get("available_at")
        if not isinstance(available_at, str) or not available_at.endswith("+08:00"):
            raise ValueError(f"TAIFEX closure lacks timezone-aware conservative availability: {day}")
        result[day] = item
    return result


def is_taifex_trading_day(value: date) -> bool:
    if value in load_temporary_closures():
        return False
    sessions, _ = get_trading_days_in_range(value, value)
    return bool(sessions)


def get_taifex_trading_days_in_range(start: date, end: date) -> tuple[list[date], list[date]]:
    sessions, closed = get_trading_days_in_range(start, end)
    temporary = load_temporary_closures()
    return [day for day in sessions if day not in temporary], sorted([*closed, *(day for day in sessions if day in temporary)])


def get_next_taifex_trading_day(value: date) -> date | None:
    """Find the next TAIFEX session, preserving official calendar coverage checks."""
    candidate = value + timedelta(days=1)
    # The underlying calendar checks every date and raises on unsupported years.
    while True:
        try:
            if is_taifex_trading_day(candidate):
                return candidate
        except ValueError:
            return None
        candidate += timedelta(days=1)


def get_previous_taifex_trading_day(value: date) -> date | None:
    """Find the prior TAIFEX session, including temporary closure overrides."""
    candidate = value - timedelta(days=1)
    while True:
        try:
            if is_taifex_trading_day(candidate):
                return candidate
        except ValueError:
            return None
        candidate -= timedelta(days=1)


def taifex_session_arrangements(day: date) -> dict[str, dict[str, str]]:
    """Expected target-day sessions from the TAIFEX, not TWSE, calendar."""
    closure = load_temporary_closures().get(day)
    if closure:
        return {
            session: {"state": "not_scheduled", "evidence": closure["source"]}
            for session in ("regular", "after_hours")
        }
    if not is_taifex_trading_day(day):
        return {
            session: {"state": "not_scheduled", "evidence": "TAIFEX trading calendar"}
            for session in ("regular", "after_hours")
        }
    prior = day - timedelta(days=1)
    while not is_taifex_trading_day(prior):
        prior -= timedelta(days=1)
    return {
        "regular": {"state": "expected", "evidence": "TAIFEX trading calendar"},
        "after_hours": {
            "state": "expected",
            "evidence": f"TAIFEX after-hours attribution from {prior.isoformat()} 15:00",
        },
    }
