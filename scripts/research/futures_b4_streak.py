"""PIT-safe same-contract TX regular-session streak features for B4."""
from __future__ import annotations

from datetime import date, datetime
from typing import Any, Callable, Iterable, Mapping

from taifex_trading_calendar import get_previous_taifex_trading_day


RED_COLUMN = "tx_regular_red_streak"
BLACK_COLUMN = "tx_regular_black_streak"
AVAILABLE_COLUMN = "tx_regular_streak_available_at"
STREAK_COLUMNS = (RED_COLUMN, BLACK_COLUMN)


def _aware(value: Any, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value


def _return(row: Mapping[str, Any]) -> float:
    try:
        open_price, close_price = float(row["target_open"]), float(row["target_close"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("B4 streak source lacks target open/close") from exc
    if open_price == 0:
        raise ValueError("B4 streak source has zero target open")
    return close_price / open_price - 1.0


def attach_streak_features(
    rows: Iterable[Mapping[str, Any]], *,
    previous_session: Callable[[date], date | None] = get_previous_taifex_trading_day,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Attach pre-08:30 streaks without using the current day's target.

    A streak state is only extended across the immediately previous official
    session with the same deterministic contract.  Missing sessions or source
    availability break the chain to null; flat days and contract rolls reset it
    to zero by definition.
    """
    copied = [dict(row) for row in rows]
    by_day = {row.get("prediction_date"): row for row in copied}
    if len(by_day) != len(copied) or any(type(day) is not date for day in by_day):
        raise ValueError("B4 streak rows require unique date prediction_date")
    states: dict[date, dict[str, Any] | None] = {}
    for day in sorted(by_day):
        row = by_day[day]
        prediction_at = _aware(row.get("prediction_at"), "prediction_at")
        target_at = row.get("target_available_at")
        if target_at is None:
            states[day] = None
            continue
        target_at = _aware(target_at, "target_available_at")
        prior_day = previous_session(day)
        prior = by_day.get(prior_day)
        result = _return(row)
        # A flat completed session resets both counters even if earlier history
        # is unavailable; it cannot carry an unobserved streak forward.
        if result == 0:
            states[day] = {"red": 0, "black": 0, "available_at": target_at}
            continue
        if prior is None or prior.get("target_available_at") is None:
            states[day] = None
            continue
        prior_state = states.get(prior_day)
        if prior.get("target_contract") != row.get("target_contract"):
            states[day] = {
                "red": 1 if result > 0 else 0, "black": 1 if result < 0 else 0,
                "available_at": target_at,
            }
            continue
        if prior_state is None:
            states[day] = None
            continue
        available_at = max(target_at, prior_state["available_at"])
        states[day] = {
            "red": prior_state["red"] + 1 if result > 0 else 0,
            "black": prior_state["black"] + 1 if result < 0 else 0,
            "available_at": available_at,
        }
    output: list[dict[str, Any]] = []
    coverage = {"known": 0, "null": 0}
    for day in sorted(by_day):
        row = by_day[day]
        prediction_at = _aware(row["prediction_at"], "prediction_at")
        prior_day = previous_session(day)
        prior = by_day.get(prior_day)
        state = states.get(prior_day)
        row[RED_COLUMN] = row[BLACK_COLUMN] = row[AVAILABLE_COLUMN] = None
        if prior is not None and prior.get("target_available_at") is not None:
            prior_available = _aware(prior["target_available_at"], "target_available_at")
            if row.get("target_contract") != prior.get("target_contract"):
                row[RED_COLUMN], row[BLACK_COLUMN], row[AVAILABLE_COLUMN] = 0, 0, prior_available
            elif state is not None and state["available_at"] <= prediction_at:
                row[RED_COLUMN] = state["red"]
                row[BLACK_COLUMN] = state["black"]
                row[AVAILABLE_COLUMN] = state["available_at"]
        if row[AVAILABLE_COLUMN] is not None and row[AVAILABLE_COLUMN] > prediction_at:
            raise ValueError(f"Streak feature exceeds prediction_at: {day}")
        coverage["known" if row[AVAILABLE_COLUMN] is not None else "null"] += 1
        output.append(row)
    return output, {
        "columns": list(STREAK_COLUMNS),
        "definition": {
            "return": "same deterministic TX contract close / open - 1 on completed regular session",
            "red": "> 0 consecutive completed sessions", "black": "< 0 consecutive completed sessions",
            "flat": "= 0 resets both to 0", "roll": "contract change resets both to 0",
            "gap": "missing immediately prior official session or unavailable chain produces null",
            "availability": "maximum target_available_at over the historical streak chain",
            "current_target": "not used",
        },
        "coverage": coverage,
        "source": "B4 deterministic target_contract and transaction open/close fields",
    }
